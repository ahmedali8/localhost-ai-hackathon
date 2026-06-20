/*
 * cardputer.ino — Capture Terminal firmware (project-name-agnostic)
 * ------------------------------------------------------------------
 * The Cardputer is a dumb I/O terminal tethered to a Mac over USB-CDC serial.
 * Capture a thought two ways:
 *   - TYPE on the keyboard  -> Enter sends a (keyboard) note
 *   - HOLD-toggle the SIDE BUTTON (G0/BtnA) -> records the mic, sends (voice) audio
 * The Mac saves text / transcribes audio and sends short status lines back.
 *
 * WIRE PROTOCOL (newline-delimited JSON):
 *   device -> host:
 *     {"type":"note","src":"keyboard","text":"..."}
 *     {"type":"rec_start"}
 *     {"type":"audio","seq":N,"b64":"<pcm16le base64>","last":<bool>}
 *     {"type":"rec_end"}
 *   host -> device:
 *     {"status":"<<=18 chars>"}   e.g. SAVED, TRANSCRIBING, ERR
 *     {"echo":"<text to show in LAST>"}
 *     {"count":<int>}
 *
 * Audio: 16 kHz mono int16, recorded to PSRAM, sent AFTER stop as base64 chunks (cap 30 s).
 * Build: USB Mode = Hardware CDC+JTAG, CDC On Boot = Enabled (both default for m5stack_cardputer).
 * Libs:  M5Cardputer, ArduinoJson v7.
 */

#include <M5Cardputer.h>
#include <ArduinoJson.h>
#include "mbedtls/base64.h"

// ---- layout ----------------------------------------------------------------
static const int DISP_W = 240, DISP_H = 135;
static const int BAR_H = 14, IN_H = 16;
static const int BODY_Y = BAR_H, BODY_H = DISP_H - BAR_H - IN_H, IN_Y = DISP_H - IN_H;
static const int COLS = 40;

// ---- audio config ----------------------------------------------------------
static const uint32_t SR        = 16000;             // 16 kHz mono
static const int      REC_SECS  = 30;                // hard cap
static const size_t   REC_CAP   = (size_t)SR * REC_SECS;  // samples
static const size_t   MIC_CHUNK = 1600;              // 0.1 s per async record() call
static const size_t   TX_RAW    = 2400;              // raw bytes per audio frame (~3.2KB b64)

// ---- state (heap-safe char[]; audio in PSRAM) ------------------------------
M5Canvas body(&M5Cardputer.Display);
char   input[256];   int inLen = 0;
char   last[256] = "(nothing yet)";
char   state[20] = "idle";
int    noteCount = 0;
char   rx[1024];     int rxLen = 0;

int16_t* recBuf = nullptr;     // PSRAM record buffer
size_t   recLen = 0;           // samples captured
bool     recording = false;
bool     chunkPending = false;
uint32_t recStartMs = 0;

// ---- drawing ---------------------------------------------------------------
void drawBar() {
  uint16_t bg = recording ? TFT_RED : TFT_DARKGREEN;
  M5Cardputer.Display.fillRect(0, 0, DISP_W, BAR_H, bg);
  M5Cardputer.Display.setTextSize(1);
  M5Cardputer.Display.setTextColor(TFT_WHITE, bg);
  char l[48]; snprintf(l, sizeof(l), "notes:%d", noteCount);
  M5Cardputer.Display.drawString(l, 3, 3);
  int x = DISP_W - (int)strlen(state) * 6 - 4;
  M5Cardputer.Display.drawString(state, x, 3);
}

void drawBody() {
  body.fillSprite(TFT_BLACK);
  body.setTextSize(1);
  body.setTextColor(TFT_CYAN, TFT_BLACK);
  body.setCursor(0, 0); body.print("LAST:\n");
  body.setTextColor(TFT_WHITE, TFT_BLACK);
  int col = 0;
  for (const char* p = last; *p; ++p) {
    if (col >= COLS) { body.print('\n'); col = 0; }
    body.print(*p); col++;
  }
  body.pushSprite(0, BODY_Y);
}

void drawInput() {
  M5Cardputer.Display.fillRect(0, IN_Y, DISP_W, IN_H, TFT_NAVY);
  M5Cardputer.Display.setTextSize(1);
  M5Cardputer.Display.setTextColor(TFT_WHITE, TFT_NAVY);
  const char* d = input; if (inLen > 37) d += (inLen - 37);
  char buf[44]; snprintf(buf, sizeof(buf), "> %s_", d);
  M5Cardputer.Display.drawString(buf, 3, IN_Y + 4);
}

void redraw() { drawBar(); drawBody(); drawInput(); }
void setState(const char* s) { strncpy(state, s, sizeof(state) - 1); state[sizeof(state) - 1] = '\0'; drawBar(); }

// ---- serial out ------------------------------------------------------------
void sendType(const char* t) {
  JsonDocument d; d["type"] = t;
  serializeJson(d, Serial); Serial.print('\n');
}

void sendNote() {
  JsonDocument d; d["type"] = "note"; d["src"] = "keyboard"; d["text"] = input;
  serializeJson(d, Serial); Serial.print('\n');
}

void sendAudio() {
  uint8_t* raw = (uint8_t*)recBuf;
  size_t total = recLen * sizeof(int16_t);
  static char b64[3600];
  int seq = 0;
  if (total == 0) { JsonDocument d; d["type"]="audio"; d["seq"]=0; d["b64"]=""; d["last"]=true;
                    serializeJson(d, Serial); Serial.print('\n'); return; }
  for (size_t off = 0; off < total; off += TX_RAW) {
    size_t n = (TX_RAW < total - off) ? TX_RAW : (total - off);
    size_t olen = 0;
    mbedtls_base64_encode((unsigned char*)b64, sizeof(b64), &olen, raw + off, n);
    b64[olen] = '\0';
    JsonDocument d; d["type"]="audio"; d["seq"]=seq++; d["b64"]=b64; d["last"]=(off + TX_RAW >= total);
    serializeJson(d, Serial); Serial.print('\n');
    delay(2);  // let the host drain
  }
}

// ---- serial in -------------------------------------------------------------
void handleLine(const char* line) {
  JsonDocument d;
  if (deserializeJson(d, line)) return;
  if (d["status"].is<const char*>()) setState(d["status"].as<const char*>());
  if (d["echo"].is<const char*>()) {
    strncpy(last, d["echo"].as<const char*>(), sizeof(last) - 1); last[sizeof(last)-1]='\0'; drawBody();
  }
  if (d["count"].is<int>()) { noteCount = d["count"].as<int>(); drawBar(); }
}

void handleSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') { rx[rxLen] = '\0'; handleLine(rx); rxLen = 0; }
    else if (rxLen < (int)sizeof(rx) - 1) rx[rxLen++] = c;
  }
}

// ---- keyboard --------------------------------------------------------------
void handleKeys() {
  if (!M5Cardputer.Keyboard.isChange() || !M5Cardputer.Keyboard.isPressed()) return;
  auto ks = M5Cardputer.Keyboard.keysState();
  bool dirty = false;
  for (char c : ks.word) if (inLen < (int)sizeof(input) - 1) { input[inLen++] = c; input[inLen] = '\0'; dirty = true; }
  if (ks.del && inLen > 0) { input[--inLen] = '\0'; dirty = true; }
  if (ks.enter && inLen > 0) {
    sendNote(); inLen = 0; input[0] = '\0'; setState("saving"); dirty = true;
  }
  if (dirty) drawInput();
}

// ---- mic recording (side button toggles) -----------------------------------
void startRec() {
  if (!recBuf || recording) return;
  M5Cardputer.Speaker.end();         // mic + speaker share I2S
  M5Cardputer.Mic.begin();
  recLen = 0; chunkPending = false; recording = true; recStartMs = millis();
  sendType("rec_start");
  setState("REC 0");
}

void stopRec() {
  if (!recording) return;
  recording = false;
  while (M5Cardputer.Mic.isRecording()) delay(1);  // let the in-flight chunk finish
  if (chunkPending) { recLen += MIC_CHUNK; chunkPending = false; }
  M5Cardputer.Mic.end();
  setState("sending");
  sendAudio();
  sendType("rec_end");
}

void pumpRecording() {
  if (!recording) return;
  if (!M5Cardputer.Mic.isRecording()) {
    if (chunkPending) { recLen += MIC_CHUNK; chunkPending = false; }   // prev chunk done
    if (recLen + MIC_CHUNK <= REC_CAP) {
      M5Cardputer.Mic.record(recBuf + recLen, MIC_CHUNK, SR);
      chunkPending = true;
    } else { stopRec(); return; }
  }
  uint32_t secs = (millis() - recStartMs) / 1000;
  char s[20]; snprintf(s, sizeof(s), "REC %lu", (unsigned long)secs);
  if (strcmp(s, state) != 0) setState(s);
}

void handleButton() {
  if (M5Cardputer.BtnA.wasPressed()) { recording ? stopRec() : startRec(); }
}

// ---- lifecycle -------------------------------------------------------------
void setup() {
  auto cfg = M5.config();
  M5Cardputer.begin(cfg, true);
  Serial.begin(115200);                 // baud ignored on native USB-CDC
  M5Cardputer.Display.setRotation(1);
  M5Cardputer.Display.fillScreen(TFT_BLACK);
  if (!body.createSprite(DISP_W, BODY_H)) M5Cardputer.Display.drawString("SPRITE FAIL", 4, 40);
  recBuf = (int16_t*)heap_caps_malloc(REC_CAP * sizeof(int16_t), MALLOC_CAP_SPIRAM);
  if (!recBuf) strncpy(last, "(no PSRAM: voice disabled)", sizeof(last));
  input[0] = '\0';
  redraw();
}

void loop() {
  M5Cardputer.update();   // refreshes keyboard + BtnA
  handleKeys();
  handleButton();
  pumpRecording();
  handleSerial();
}
