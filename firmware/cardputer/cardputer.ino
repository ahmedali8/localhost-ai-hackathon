/*
 * cardputer.ino — Capture Terminal firmware (project-name-agnostic)
 * ------------------------------------------------------------------
 * The M5Stack Cardputer is a DUMB I/O terminal tethered to a Mac over
 * USB-CDC serial. It does NO AI itself. You type (and later, speak) a
 * "capture"; it ships the text to the Mac, which saves it. The Mac sends
 * back short status lines that this firmware renders on the 240x135 screen.
 *
 * WIRE PROTOCOL (newline-delimited JSON, both directions):
 *   device -> host:
 *     {"type":"note","src":"keyboard","text":"<what you typed>"}
 *     // (step 2, mic) {"type":"rec_start"} / {"type":"audio",...} / {"type":"rec_end"}
 *   host -> device:
 *     {"status":"<<=18 chars>"}   // e.g. SAVED, LISTENING, TRANSCRIBING, ERR
 *     {"echo":"<text to show in the LAST pane>"}
 *     {"count":<int>}             // total captures so far
 *
 * SCREEN LAYOUT (240x135, ~40 cols):
 *   [ status bar ]  mode | notes:N | <state>
 *   [ body pane  ]  LAST: <most recent capture, wrapped>
 *   [ input line ]  > <what you're typing>_
 *
 * BUILD: set ARDUINO_USB_CDC_ON_BOOT=1 (USB-C is native CDC, not a UART bridge).
 * LIBS:  M5Cardputer, ArduinoJson v7.
 */

#include <M5Cardputer.h>
#include <ArduinoJson.h>

// ---- layout ----------------------------------------------------------------
static const int DISP_W = 240, DISP_H = 135;
static const int BAR_H  = 14;                 // top status bar
static const int IN_H   = 16;                 // bottom input line
static const int BODY_Y = BAR_H;
static const int BODY_H = DISP_H - BAR_H - IN_H;
static const int IN_Y   = DISP_H - IN_H;
static const int COLS   = 40;                 // chars per line at textsize 1

// ---- state (heap-safe: fixed char[], never Arduino String) -----------------
M5Canvas body(&M5Cardputer.Display);          // body pane sprite
char  input[256];      int inLen = 0;         // current typed line
char  last[256]    = "(nothing yet)";         // most recent capture (echoed back)
char  state[20]    = "idle";                  // status word shown in the bar
int   noteCount    = 0;
char  rx[1024];        int rxLen = 0;          // serial line accumulator

// ---- MIC (step 2 — stubbed, OFF) -------------------------------------------
// Plan: the Cardputer SIDE BUTTON (G0 / M5Cardputer.BtnA) toggles voice capture.
// On press -> {"type":"rec_start"}; read SPM1423 via M5Cardputer.Mic (NOT raw I2S),
// base64 PCM16 chunks -> {"type":"audio",...}; on press again -> {"type":"rec_end"}.
// Host runs Whisper and returns {"echo": transcript}. Wired in step 2.
#define ENABLE_MIC 0

// ---- drawing ---------------------------------------------------------------
void drawBar() {
  M5Cardputer.Display.fillRect(0, 0, DISP_W, BAR_H, TFT_DARKGREEN);
  M5Cardputer.Display.setTextSize(1);
  M5Cardputer.Display.setTextColor(TFT_WHITE, TFT_DARKGREEN);
  char line[48];
  snprintf(line, sizeof(line), "TYPE  notes:%d", noteCount);
  M5Cardputer.Display.drawString(line, 3, 3);
  // right-aligned state word
  int x = DISP_W - (int)strlen(state) * 6 - 4;
  M5Cardputer.Display.drawString(state, x, 3);
}

// word-wrap `last` into the body sprite
void drawBody() {
  body.fillSprite(TFT_BLACK);
  body.setTextSize(1);
  body.setTextColor(TFT_CYAN, TFT_BLACK);
  body.setCursor(0, 0);
  body.print("LAST:\n");
  body.setTextColor(TFT_WHITE, TFT_BLACK);
  int col = 0;
  for (const char* p = last; *p; ++p) {
    if (col >= COLS) { body.print('\n'); col = 0; }
    body.print(*p);
    col++;
  }
  body.pushSprite(0, BODY_Y);
}

void drawInput() {
  M5Cardputer.Display.fillRect(0, IN_Y, DISP_W, IN_H, TFT_NAVY);
  M5Cardputer.Display.setTextSize(1);
  M5Cardputer.Display.setTextColor(TFT_WHITE, TFT_NAVY);
  // show the tail if the line is longer than the screen
  const char* d = input;
  if (inLen > 37) d += (inLen - 37);
  char buf[44];
  snprintf(buf, sizeof(buf), "> %s_", d);
  M5Cardputer.Display.drawString(buf, 3, IN_Y + 4);
}

void redraw() { drawBar(); drawBody(); drawInput(); }

// ---- serial out ------------------------------------------------------------
void sendNote() {
  JsonDocument d;
  d["type"] = "note";
  d["src"]  = "keyboard";
  d["text"] = input;
  serializeJson(d, Serial);
  Serial.print('\n');
}

// ---- serial in -------------------------------------------------------------
void handleLine(const char* line) {
  JsonDocument d;
  if (deserializeJson(d, line)) return;            // ignore malformed
  if (d["status"].is<const char*>()) {
    strncpy(state, d["status"].as<const char*>(), sizeof(state) - 1);
    state[sizeof(state) - 1] = '\0';
  }
  if (d["echo"].is<const char*>()) {
    strncpy(last, d["echo"].as<const char*>(), sizeof(last) - 1);
    last[sizeof(last) - 1] = '\0';
  }
  if (d["count"].is<int>()) noteCount = d["count"].as<int>();
  redraw();
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

  for (char c : ks.word) {
    if (inLen < (int)sizeof(input) - 1) { input[inLen++] = c; input[inLen] = '\0'; dirty = true; }
  }
  if (ks.del && inLen > 0) { input[--inLen] = '\0'; dirty = true; }
  if (ks.enter && inLen > 0) {
    sendNote();
    inLen = 0; input[0] = '\0';
    strncpy(state, "saving", sizeof(state)); dirty = true;
  }
  if (dirty) { drawBar(); drawInput(); }
}

// ---- lifecycle -------------------------------------------------------------
void setup() {
  auto cfg = M5.config();
  M5Cardputer.begin(cfg, true);                 // true = enable keyboard
  Serial.begin(115200);                         // baud ignored on native USB-CDC
  M5Cardputer.Display.setRotation(1);
  M5Cardputer.Display.fillScreen(TFT_BLACK);
  if (!body.createSprite(DISP_W, BODY_H)) {     // guard OOM
    M5Cardputer.Display.drawString("SPRITE FAIL", 4, 40);
  }
  input[0] = '\0';
  redraw();
}

void loop() {
  M5Cardputer.update();
  handleKeys();
  handleSerial();
}
