/*=============================================================================
 *  cardputer.ino  —  Capture Terminal firmware
 *=============================================================================
 *
 *  WHAT THIS IS
 *  ------------
 *  Firmware for an M5Stack Cardputer used as a *dumb I/O terminal* for a host
 *  Mac. It captures short thoughts two ways and ships them to the Mac over a
 *  USB-CDC serial link; the Mac stores/transcribes them and sends back short
 *  status lines that this firmware renders on the 240x135 screen.
 *
 *      TYPE on the keyboard, press Enter ............ sends a (keyboard) note
 *      Press Enter on an EMPTY line, or tap G0 ...... toggles voice recording
 *
 *  No intelligence runs here — the device only captures input and renders
 *  status. All storage / speech-to-text happens on the Mac.
 *
 *  KEY DESIGN DECISIONS (and why)
 *  ------------------------------
 *  1. Audio is STREAMED, never fully buffered.
 *     This unit (Cardputer ADV / StampS3) has *no usable PSRAM*, so a multi-
 *     second 16-bit buffer cannot be allocated. Instead we read the mic in
 *     0.1 s chunks and send each chunk immediately. Two small chunk buffers
 *     are used round-robin (double-buffering) so the mic keeps capturing while
 *     the previous chunk is being base64'd and written to serial — no gaps,
 *     constant ~6 KB RAM, unlimited recording length.
 *  2. Two record triggers. The side button (G0/BtnA) is the intended gesture,
 *     but its mapping can vary by board revision, so "Enter on an empty line"
 *     is provided as an always-works keyboard fallback.
 *  3. Heap hygiene. Fixed char[] buffers (never Arduino String, which
 *     fragments the small heap), function-local JsonDocuments (freed promptly),
 *     and a single static base64 scratch buffer. The display is split into
 *     three regions redrawn independently so we never repaint the whole screen.
 *
 *  WIRE PROTOCOL  (newline-delimited JSON, UTF-8)
 *  ---------------------------------------------
 *    device -> host:
 *      {"type":"note","src":"keyboard","text":"<typed text>"}
 *      {"type":"rec_start"}                                   // recording began
 *      {"type":"audio","seq":<n>,"b64":"<pcm16le base64>","last":false}
 *      {"type":"rec_end"}                                     // recording ended
 *    host -> device:
 *      {"status":"<=18 chars>"}   // shown right-aligned in the status bar
 *      {"echo":"<text>"}          // shown in the LAST pane
 *      {"count":<int>}            // total captures, shown in the status bar
 *
 *  AUDIO FORMAT: 16 kHz, mono, signed 16-bit little-endian PCM.
 *
 *  BUILD / FLASH
 *  -------------
 *    Board : esp32:esp32:m5stack_cardputer   (defaults are correct:
 *            USB Mode = Hardware CDC+JTAG, USB CDC On Boot = Enabled)
 *    Libs  : M5Cardputer, ArduinoJson v7
 *    Flash : arduino-cli compile -b esp32:esp32:m5stack_cardputer \
 *              --upload -p /dev/cu.usbmodem* firmware/cardputer
 *
 *  FILE MAP (sections below, in order)
 *    [1] Configuration      [2] Global state     [3] Display
 *    [4] Serial TX          [5] Serial RX        [6] Audio capture
 *    [7] Keyboard           [8] Side button      [9] Arduino lifecycle
 *===========================================================================*/

#include <M5Cardputer.h>
#include <ArduinoJson.h>
#include "mbedtls/base64.h"   // hardware-friendly base64 from the ESP-IDF

/*-----------------------------------------------------------------------------
 * [1] CONFIGURATION  — all tunables live here; no magic numbers below.
 *---------------------------------------------------------------------------*/

// Display geometry (landscape 240x135). The screen is split top-to-bottom into
// a status bar, a body pane (last capture), and an input line.
static constexpr int DISP_W = 240;
static constexpr int DISP_H = 135;
static constexpr int BAR_H  = 14;                       // status bar height
static constexpr int IN_H   = 16;                       // input line height
static constexpr int BODY_Y = BAR_H;                    // body pane top
static constexpr int BODY_H = DISP_H - BAR_H - IN_H;    // body pane height
static constexpr int IN_Y   = DISP_H - IN_H;            // input line top
static constexpr int COLS   = 40;                       // chars per line @ textsize 1 (~6 px glyphs)

// Audio. 16 kHz mono int16 is what Whisper wants. CHUNK is the per-read size:
// 1600 samples = 0.1 s = 3200 bytes raw -> ~4267 base64 chars per frame.
static constexpr uint32_t SR     = 16000;
static constexpr size_t   CHUNK  = 1600;

// Serial. The baud value is ignored on native USB-CDC (throughput is USB
// full-speed), but a value is still required by Serial.begin().
static constexpr uint32_t BAUD   = 115200;

// Fixed buffer sizes.
static constexpr size_t IN_MAX   = 256;    // max typed-line length
static constexpr size_t LAST_MAX = 256;    // max "last capture" text shown
static constexpr size_t RX_MAX   = 1024;   // max inbound serial line
static constexpr size_t B64_MAX  = 4400;   // base64 of one CHUNK + NUL headroom

/*-----------------------------------------------------------------------------
 * [2] GLOBAL STATE
 *---------------------------------------------------------------------------*/

// Offscreen sprite for the body pane so we can word-wrap + repaint it alone.
static M5Canvas body(&M5Cardputer.Display);

// Input line (typed text). char[] + length, never String (heap hygiene).
static char input[IN_MAX];
static int  inLen = 0;

// UI text shown in the body pane (host echoes captures here). Doubles as the
// on-screen help on boot.
static char last[LAST_MAX] = "type+Enter=note  |  empty Enter / G0 = record";

// Right-aligned status word in the bar (host-driven: SAVED, TRANSCRIBING, ...).
static char status[20] = "idle";

// Running capture count (host-driven).
static int noteCount = 0;

// Inbound serial line assembler.
static char rxBuf[RX_MAX];
static int  rxLen = 0;

// Audio capture state. Two chunk buffers used round-robin while recording.
static int16_t  chunkA[CHUNK];
static int16_t  chunkB[CHUNK];
static int16_t* fillBuf   = chunkA;   // buffer the mic is currently filling
static bool     recording = false;
static int      audioSeq  = 0;        // monotonic frame counter per recording
static uint32_t recStartMs = 0;       // for the elapsed-seconds readout

/*-----------------------------------------------------------------------------
 * [3] DISPLAY  — three independently repainted regions (cheap partial redraws).
 *---------------------------------------------------------------------------*/

/// Paint the top status bar: "notes:N" left, status word right.
/// Background turns red while recording so the state is obvious at a glance.
static void drawBar() {
  const uint16_t bg = recording ? TFT_RED : TFT_DARKGREEN;
  M5Cardputer.Display.fillRect(0, 0, DISP_W, BAR_H, bg);
  M5Cardputer.Display.setTextSize(1);
  M5Cardputer.Display.setTextColor(TFT_WHITE, bg);
  char left[32];
  snprintf(left, sizeof(left), "notes:%d", noteCount);
  M5Cardputer.Display.drawString(left, 3, 3);
  const int x = DISP_W - static_cast<int>(strlen(status)) * 6 - 4;  // right-align
  M5Cardputer.Display.drawString(status, x, 3);
}

/// Paint the body pane: the last capture, hard-wrapped at COLS into the sprite.
static void drawBody() {
  body.fillSprite(TFT_BLACK);
  body.setTextSize(1);
  body.setTextColor(TFT_CYAN, TFT_BLACK);
  body.setCursor(0, 0);
  body.print("LAST:\n");
  body.setTextColor(TFT_WHITE, TFT_BLACK);
  int col = 0;
  for (const char* p = last; *p; ++p) {
    if (col >= COLS) { body.print('\n'); col = 0; }   // manual wrap (sprite clips otherwise)
    body.print(*p);
    ++col;
  }
  body.pushSprite(0, BODY_Y);
}

/// Paint the bottom input line, showing a trailing window of the typed text.
static void drawInput() {
  M5Cardputer.Display.fillRect(0, IN_Y, DISP_W, IN_H, TFT_NAVY);
  M5Cardputer.Display.setTextSize(1);
  M5Cardputer.Display.setTextColor(TFT_WHITE, TFT_NAVY);
  const char* tail = input;
  if (inLen > 37) tail += (inLen - 37);               // keep the cursor visible
  char line[44];
  snprintf(line, sizeof(line), "> %s_", tail);
  M5Cardputer.Display.drawString(line, 3, IN_Y + 4);
}

static void redrawAll() { drawBar(); drawBody(); drawInput(); }

/// Update the status word and repaint just the bar.
static void setStatus(const char* s) {
  strncpy(status, s, sizeof(status) - 1);
  status[sizeof(status) - 1] = '\0';
  drawBar();
}

/*-----------------------------------------------------------------------------
 * [4] SERIAL TX  — emit one NDJSON object per call.
 *---------------------------------------------------------------------------*/

/// Send a bare typed message, e.g. {"type":"rec_start"}.
static void sendType(const char* type) {
  JsonDocument doc;
  doc["type"] = type;
  serializeJson(doc, Serial);
  Serial.print('\n');
}

/// Send the current input line as a keyboard note.
static void sendNote() {
  JsonDocument doc;
  doc["type"] = "note";
  doc["src"]  = "keyboard";
  doc["text"] = input;
  serializeJson(doc, Serial);
  Serial.print('\n');
}

/// Base64-encode one PCM chunk and send it as an {"type":"audio",...} frame.
static void sendAudioChunk(const int16_t* buf, size_t samples) {
  static char b64[B64_MAX];                            // static: no per-frame heap churn
  size_t outLen = 0;
  mbedtls_base64_encode(reinterpret_cast<unsigned char*>(b64), sizeof(b64),
                        &outLen, reinterpret_cast<const uint8_t*>(buf),
                        samples * sizeof(int16_t));
  b64[outLen] = '\0';
  JsonDocument doc;
  doc["type"] = "audio";
  doc["seq"]  = audioSeq++;
  doc["b64"]  = b64;
  doc["last"] = false;                                 // host uses rec_end as the terminator
  serializeJson(doc, Serial);
  Serial.print('\n');
}

/*-----------------------------------------------------------------------------
 * [5] SERIAL RX  — assemble lines, apply host status frames to the UI.
 *---------------------------------------------------------------------------*/

/// Apply one inbound JSON line: {"status"}, {"echo"}, and/or {"count"}.
static void handleLine(const char* line) {
  JsonDocument doc;
  if (deserializeJson(doc, line)) return;              // ignore malformed lines
  if (doc["status"].is<const char*>()) {
    setStatus(doc["status"].as<const char*>());
  }
  if (doc["echo"].is<const char*>()) {
    strncpy(last, doc["echo"].as<const char*>(), sizeof(last) - 1);
    last[sizeof(last) - 1] = '\0';
    drawBody();
  }
  if (doc["count"].is<int>()) {
    noteCount = doc["count"].as<int>();
    drawBar();
  }
}

/// Drain available serial bytes into rxBuf, dispatching on each newline.
static void handleSerial() {
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      rxBuf[rxLen] = '\0';
      handleLine(rxBuf);
      rxLen = 0;
    } else if (rxLen < static_cast<int>(RX_MAX) - 1) {
      rxBuf[rxLen++] = c;
    }
    // Overflow bytes are dropped on purpose — keeps RX bounded and heap-safe.
  }
}

/*-----------------------------------------------------------------------------
 * [6] AUDIO CAPTURE (streamed, double-buffered)
 *
 *  Lifecycle: startRec() -> pumpRecording() each loop -> stopRec().
 *  The mic and speaker share an I2S peripheral, so the speaker is stopped for
 *  the duration of a recording.
 *---------------------------------------------------------------------------*/

/// Begin a recording: free I2S from the speaker, start the mic, kick the first chunk.
static void startRec() {
  if (recording) return;
  M5Cardputer.Speaker.end();                           // release shared I2S
  if (!M5Cardputer.Mic.begin()) { setStatus("MIC ERR"); return; }
  recording  = true;
  audioSeq   = 0;
  recStartMs = millis();
  sendType("rec_start");
  fillBuf = chunkA;
  M5Cardputer.Mic.record(fillBuf, CHUNK, SR);          // async fill begins
  setStatus("REC 0");
}

/// End a recording: flush the in-flight chunk, stop the mic, tell the host.
static void stopRec() {
  if (!recording) return;
  recording = false;
  while (M5Cardputer.Mic.isRecording()) delay(1);      // let the last chunk finish
  sendAudioChunk(fillBuf, CHUNK);                       // ship it
  M5Cardputer.Mic.end();
  sendType("rec_end");
  setStatus("sending");
}

/// Per-loop pump: when the current chunk finishes, immediately start the next
/// (into the other buffer) so capture is gapless, then send the finished one.
static void pumpRecording() {
  if (!recording) return;
  if (M5Cardputer.Mic.isRecording()) return;           // still filling — nothing to do

  int16_t* finished = fillBuf;
  fillBuf = (fillBuf == chunkA) ? chunkB : chunkA;      // swap buffers
  M5Cardputer.Mic.record(fillBuf, CHUNK, SR);           // keep capturing
  sendAudioChunk(finished, CHUNK);                      // ship the finished chunk

  const uint32_t secs = (millis() - recStartMs) / 1000; // live elapsed readout
  char s[20];
  snprintf(s, sizeof(s), "REC %lu", static_cast<unsigned long>(secs));
  if (strcmp(s, status) != 0) setStatus(s);
}

/*-----------------------------------------------------------------------------
 * [7] KEYBOARD  — build the input line; Enter sends a note or toggles recording.
 *---------------------------------------------------------------------------*/

static void handleKeys() {
  // isChange() gate prevents key floods while a key is held.
  if (!M5Cardputer.Keyboard.isChange() || !M5Cardputer.Keyboard.isPressed()) return;

  const auto ks = M5Cardputer.Keyboard.keysState();
  bool dirty = false;

  for (const char c : ks.word) {                       // printable characters
    if (inLen < static_cast<int>(IN_MAX) - 1) {
      input[inLen++] = c;
      input[inLen]   = '\0';
      dirty = true;
    }
  }
  if (ks.del && inLen > 0) {                            // backspace
    input[--inLen] = '\0';
    dirty = true;
  }
  if (ks.enter) {
    if (inLen > 0) {                                    // text present -> save a note
      sendNote();
      inLen = 0;
      input[0] = '\0';
      setStatus("saving");
      dirty = true;
    } else {                                            // empty line -> record toggle
      recording ? stopRec() : startRec();
    }
  }
  if (dirty) drawInput();
}

/*-----------------------------------------------------------------------------
 * [8] SIDE BUTTON (G0 / BtnA)  — alternate record toggle.
 *---------------------------------------------------------------------------*/

static void handleButton() {
  if (M5Cardputer.BtnA.wasPressed()) {
    recording ? stopRec() : startRec();
  }
}

/*-----------------------------------------------------------------------------
 * [9] ARDUINO LIFECYCLE
 *---------------------------------------------------------------------------*/

void setup() {
  auto cfg = M5.config();
  M5Cardputer.begin(cfg, /*enableKeyboard=*/true);
  Serial.begin(BAUD);                                  // baud ignored on native USB-CDC
  M5Cardputer.Display.setRotation(1);                  // landscape
  M5Cardputer.Display.fillScreen(TFT_BLACK);
  if (!body.createSprite(DISP_W, BODY_H)) {            // guard against OOM
    M5Cardputer.Display.drawString("SPRITE FAIL", 4, 40);
  }
  input[0] = '\0';
  redrawAll();
}

void loop() {
  M5Cardputer.update();   // refresh keyboard + BtnA state (must run every loop)
  handleKeys();
  handleButton();
  pumpRecording();
  handleSerial();
}
