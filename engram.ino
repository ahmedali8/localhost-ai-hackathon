/*
 * engram.ino  —  Cardputer firmware for Project Engram
 *
 * Hardware : M5Cardputer (ESP32-S3, 240x135 LCD, 56-key kbd, SPM1423 mic)
 * Protocol : NDJSON over USB-CDC  (ARDUINO_USB_CDC_ON_BOOT=1 required)
 * Libs     : M5Cardputer, ArduinoJson v7
 *
 * Build flags (platformio.ini):
 *   build_flags = -D ARDUINO_USB_CDC_ON_BOOT=1
 *
 * Wire protocol (see project spec):
 *   TX: {"cmd":"dump","text":"..."}  {"cmd":"ask","text":"..."}
 *       {"cmd":"remind","text":"...","in_s":<int>}
 *   RX: {"status":"..."}  {"t":"..."}  {"e":1}
 *       {"link":"..."}  {"notify":"..."}  {"err":"..."}
 */

// ─────────────────────────────────────────────────────────────────────────────
// MIC SPIKE — gated behind this define. Set to 1 to compile mic code.
// WARNING: keep at 0 until keyboard path is verified stable.
#define ENABLE_MIC 0
// ─────────────────────────────────────────────────────────────────────────────

#include <M5Cardputer.h>
#include <ArduinoJson.h>

// ═════════════════════════════ DISPLAY CONSTANTS ════════════════════════════

static constexpr int LCD_W       = 240;
static constexpr int LCD_H       = 135;
static constexpr int FONT_W      = 6;   // font 1 pixel width
static constexpr int FONT_H      = 8;   // font 1 pixel height
static constexpr int COLS        = 40;  // chars per line
static constexpr int HUD_H       = 10;  // HUD bar height in px
static constexpr int INPUT_H     = 10;  // input bar height in px
static constexpr int SCROLL_Y    = HUD_H;
static constexpr int SCROLL_H    = LCD_H - HUD_H - INPUT_H;
static constexpr int SCROLL_ROWS = SCROLL_H / FONT_H;  // 14 lines

// Colours (RGB565)
static constexpr uint16_t COL_BG      = TFT_BLACK;
static constexpr uint16_t COL_HUD_BG  = 0x0008;  // near-black blue
static constexpr uint16_t COL_HUD_FG  = TFT_CYAN;
static constexpr uint16_t COL_TEXT    = TFT_WHITE;
static constexpr uint16_t COL_INPUT   = TFT_GREEN;
static constexpr uint16_t COL_LINK    = TFT_YELLOW;
static constexpr uint16_t COL_NOTIFY  = TFT_MAGENTA;
static constexpr uint16_t COL_STATUS  = TFT_CYAN;
static constexpr uint16_t COL_CURSOR  = TFT_GREEN;
static constexpr uint16_t COL_MODE_D  = TFT_ORANGE;  // DUMP mode indicator
static constexpr uint16_t COL_MODE_A  = TFT_CYAN;    // ASK mode indicator

// ════════════════════════════════ STATE ═════════════════════════════════════

// Scroll buffer — ring of SCROLL_ROWS lines
struct ScrollLine {
    char    text[COLS + 1];
    uint16_t color;
};
static ScrollLine s_scroll[SCROLL_ROWS];
static int s_scroll_head = 0;    // index of next line to write
static int s_scroll_count = 0;   // how many lines filled

// Input buffer
static constexpr int INPUT_MAX = 160;
static char s_input[INPUT_MAX + 1];
static int  s_input_len = 0;

// HUD state (≤40 chars, null-terminated)
static char s_status[41] = "READY";

// Mode: false = ASK (default), true = DUMP
static bool s_dump_mode = false;

// Token accumulation for wrap
static char s_token_partial[COLS + 1];  // chars not yet flushed to a full line
static int  s_token_partial_len = 0;

// Serial RX line buffer
static constexpr int RX_BUF = 512;
static char s_rx_buf[RX_BUF];
static int  s_rx_len = 0;

// Sprite (canvas) — used only if creation succeeds
static M5Canvas* s_canvas = nullptr;
static bool      s_canvas_ok = false;

// ═══════════════════════════════ SCROLL API ══════════════════════════════════

static void scroll_push(const char* text, uint16_t color) {
    ScrollLine& line = s_scroll[s_scroll_head];
    strncpy(line.text, text, COLS);
    line.text[COLS] = '\0';
    line.color = color;
    s_scroll_head = (s_scroll_head + 1) % SCROLL_ROWS;
    if (s_scroll_count < SCROLL_ROWS) s_scroll_count++;
}

// Push text with automatic 40-char wrap
static void scroll_push_wrapped(const char* text, uint16_t color) {
    int len = strlen(text);
    int off = 0;
    while (off < len) {
        char tmp[COLS + 1];
        int chunk = len - off;
        if (chunk > COLS) chunk = COLS;
        memcpy(tmp, text + off, chunk);
        tmp[chunk] = '\0';
        scroll_push(tmp, color);
        off += chunk;
    }
    if (len == 0) {
        scroll_push("", color);
    }
}

// Append text to last partial-token line, flush when full or on newline
static void token_append(const char* tok) {
    int tlen = strlen(tok);
    for (int i = 0; i < tlen; i++) {
        char c = tok[i];
        if (c == '\n' || s_token_partial_len >= COLS) {
            s_token_partial[s_token_partial_len] = '\0';
            // If we have accumulated content, push it
            if (s_token_partial_len > 0 || c == '\n') {
                scroll_push(s_token_partial, COL_TEXT);
                s_token_partial_len = 0;
            }
            if (c == '\n') continue;
        }
        if (c >= 0x20) {  // printable only
            s_token_partial[s_token_partial_len++] = c;
        }
    }
}

// Flush any remaining partial token line (called on {"e":1})
static void token_flush() {
    if (s_token_partial_len > 0) {
        s_token_partial[s_token_partial_len] = '\0';
        scroll_push(s_token_partial, COL_TEXT);
        s_token_partial_len = 0;
    }
}

// ═══════════════════════════════ RENDERING ═══════════════════════════════════

static void draw_hud() {
    // Draw onto canvas or direct LCD
    auto* dst = s_canvas_ok ? (LovyanGFX*)s_canvas : (LovyanGFX*)&M5Cardputer.Display;

    // HUD bar background
    dst->fillRect(0, 0, LCD_W, HUD_H, COL_HUD_BG);
    dst->setTextColor(COL_HUD_FG);
    dst->setFont(&fonts::Font0);
    dst->setTextSize(1);

    // Mode badge
    const char* mode_str = s_dump_mode ? "[D]" : "[A]";
    uint16_t    mode_col = s_dump_mode ? COL_MODE_D : COL_MODE_A;
    dst->setTextColor(mode_col);
    dst->setCursor(1, 1);
    dst->print(mode_str);

    // Status text (up to 28 chars to leave room for "ENGRAM" on right)
    dst->setTextColor(COL_HUD_FG);
    dst->setCursor(20, 1);
    char truncated[29];
    strncpy(truncated, s_status, 28);
    truncated[28] = '\0';
    dst->print(truncated);

    // Title on far right
    dst->setTextColor(COL_STATUS);
    dst->setCursor(LCD_W - 6 * 6 - 1, 1);  // 6 chars * 6px
    dst->print("ENGRAM");
}

static void draw_scroll() {
    auto* dst = s_canvas_ok ? (LovyanGFX*)s_canvas : (LovyanGFX*)&M5Cardputer.Display;

    dst->fillRect(0, SCROLL_Y, LCD_W, SCROLL_H, COL_BG);

    int total = s_scroll_count;
    if (total > SCROLL_ROWS) total = SCROLL_ROWS;

    // oldest line first in ring
    for (int row = 0; row < total; row++) {
        int idx = (s_scroll_head - total + row + SCROLL_ROWS) % SCROLL_ROWS;
        int y   = SCROLL_Y + row * FONT_H;
        dst->setTextColor(s_scroll[idx].color);
        dst->setFont(&fonts::Font0);
        dst->setTextSize(1);
        dst->setCursor(0, y);
        dst->print(s_scroll[idx].text);
    }
}

static void draw_input() {
    auto* dst = s_canvas_ok ? (LovyanGFX*)s_canvas : (LovyanGFX*)&M5Cardputer.Display;

    int y = LCD_H - INPUT_H;
    dst->fillRect(0, y, LCD_W, INPUT_H, COL_BG);
    dst->setTextColor(COL_INPUT);
    dst->setFont(&fonts::Font0);
    dst->setTextSize(1);
    dst->setCursor(0, y + 1);

    // Show up to last 39 chars of input + cursor block
    int start = s_input_len > 39 ? s_input_len - 39 : 0;
    for (int i = start; i < s_input_len; i++) {
        dst->print(s_input[i]);
    }
    // Cursor
    dst->setTextColor(COL_CURSOR);
    dst->print('_');
}

static void redraw_all() {
    if (s_canvas_ok) {
        s_canvas->fillScreen(COL_BG);
    } else {
        M5Cardputer.Display.fillScreen(COL_BG);
    }
    draw_hud();
    draw_scroll();
    draw_input();
    if (s_canvas_ok) {
        s_canvas->pushSprite(0, 0);
    }
}

// Partial update helpers (only redraw changed regions)
static void refresh_hud_and_input() {
    if (s_canvas_ok) {
        draw_hud();
        draw_input();
        s_canvas->pushSprite(0, 0);
    } else {
        draw_hud();
        draw_input();
    }
}

static void refresh_scroll() {
    if (s_canvas_ok) {
        draw_scroll();
        s_canvas->pushSprite(0, 0);
    } else {
        draw_scroll();
    }
}

// ═══════════════════════════════ TX HELPERS ══════════════════════════════════

static void send_cmd_text(const char* cmd, const char* text) {
    // Build NDJSON: {"cmd":"<cmd>","text":"<escaped text>"}\n
    // Use ArduinoJson for correct escaping
    JsonDocument doc;
    doc["cmd"]  = cmd;
    doc["text"] = text;
    char out[INPUT_MAX + 64];
    size_t n = serializeJson(doc, out, sizeof(out));
    out[n] = '\n';
    Serial.write((uint8_t*)out, n + 1);
    Serial.flush();
}

static void send_remind(const char* text, int in_s) {
    JsonDocument doc;
    doc["cmd"]  = "remind";
    doc["text"] = text;
    doc["in_s"] = in_s;
    char out[INPUT_MAX + 64];
    size_t n = serializeJson(doc, out, sizeof(out));
    out[n] = '\n';
    Serial.write((uint8_t*)out, n + 1);
    Serial.flush();
}

// ══════════════════════════════ RX DISPATCH ══════════════════════════════════

static void handle_rx_line(char* line) {
    // Trim trailing \r
    int len = strlen(line);
    while (len > 0 && (line[len-1] == '\r' || line[len-1] == '\n')) {
        line[--len] = '\0';
    }
    if (len == 0) return;

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, line);
    if (err) return;  // malformed — ignore

    // {"status":"..."}
    if (doc["status"].is<const char*>()) {
        const char* st = doc["status"].as<const char*>();
        strncpy(s_status, st, 40);
        s_status[40] = '\0';
        refresh_hud_and_input();
        return;
    }

    // {"t":"<token>"}
    if (doc["t"].is<const char*>()) {
        const char* tok = doc["t"].as<const char*>();
        // Cap to 200 chars per frame as per spec
        char capped[201];
        strncpy(capped, tok, 200);
        capped[200] = '\0';
        token_append(capped);
        refresh_scroll();
        return;
    }

    // {"e":1}  end of stream
    if (doc["e"].is<int>() && doc["e"].as<int>() == 1) {
        token_flush();
        strncpy(s_status, "READY", 40);
        scroll_push("", COL_TEXT);  // blank separator
        redraw_all();
        return;
    }

    // {"link":"<A -> B, <=40 chars>"}
    if (doc["link"].is<const char*>()) {
        const char* lnk = doc["link"].as<const char*>();
        char buf[COLS + 1];
        snprintf(buf, sizeof(buf), "LINK: %s", lnk);
        scroll_push_wrapped(buf, COL_LINK);
        refresh_scroll();
        return;
    }

    // {"notify":"<reminder text>"}
    if (doc["notify"].is<const char*>()) {
        const char* ntf = doc["notify"].as<const char*>();
        char buf[COLS + 1];
        snprintf(buf, sizeof(buf), "!! %s", ntf);
        scroll_push_wrapped(buf, COL_NOTIFY);
        strncpy(s_status, "REMINDER!", 40);
        // Beep via speaker (brief tone)
        M5Cardputer.Speaker.tone(880, 200);
        redraw_all();
        return;
    }

    // {"err":"<msg>"}
    if (doc["err"].is<const char*>()) {
        const char* emsg = doc["err"].as<const char*>();
        char buf[COLS + 1];
        snprintf(buf, sizeof(buf), "ERR: %s", emsg);
        scroll_push_wrapped(buf, TFT_RED);
        strncpy(s_status, "ERROR", 40);
        redraw_all();
        return;
    }
}

// ════════════════════════════ KEYBOARD HANDLING ══════════════════════════════

/*
 * Key mapping notes:
 *   Enter (KEY_ENTER / '\n' / '\r') — send current input
 *   Backspace / DEL                 — delete last char
 *   Fn (opt key) or '`'            — toggle DUMP / ASK mode
 *   Ctrl-R prefix then enter        — treat input as remind "what in Xs"
 *     (simple: if input starts with "!r " parse "!r <secs> <text>")
 *
 * Remind shorthand: type "!r 30 water plants" then Enter
 *   → sends {"cmd":"remind","text":"water plants","in_s":30}
 */

static void process_enter() {
    if (s_input_len == 0) return;
    s_input[s_input_len] = '\0';

    // Check for remind shorthand: starts with "!r "
    if (s_input_len > 3 && s_input[0] == '!' && s_input[1] == 'r' && s_input[2] == ' ') {
        // Parse: !r <seconds> <text>
        char* rest = s_input + 3;
        int in_s = 0;
        char* sp = strchr(rest, ' ');
        if (sp) {
            *sp = '\0';
            in_s = atoi(rest);
            *sp = ' ';
            char* remind_text = sp + 1;
            if (in_s > 0 && strlen(remind_text) > 0) {
                // Echo to scroll
                char echo[COLS + 1];
                snprintf(echo, sizeof(echo), "> REMIND %ds: %s", in_s, remind_text);
                scroll_push_wrapped(echo, COL_MODE_D);
                send_remind(remind_text, in_s);
                strncpy(s_status, "REMIND SET", 40);
                s_input_len = 0;
                redraw_all();
                return;
            }
        }
        // If parse failed, fall through to normal send
    }

    // Echo input to scroll
    char echo[INPUT_MAX + 8];
    const char* prefix = s_dump_mode ? ">D " : ">A ";
    snprintf(echo, sizeof(echo), "%s%s", prefix, s_input);
    scroll_push_wrapped(echo, s_dump_mode ? COL_MODE_D : COL_MODE_A);

    // Send JSON
    send_cmd_text(s_dump_mode ? "dump" : "ask", s_input);

    strncpy(s_status, s_dump_mode ? "SAVED" : "THINKING", 40);
    s_input_len = 0;
    redraw_all();
}

static void process_key(uint8_t key) {
    if (key == '\n' || key == '\r') {
        process_enter();
        return;
    }

    if (key == '\b' || key == 127) {  // backspace / DEL
        if (s_input_len > 0) {
            s_input_len--;
            refresh_hud_and_input();
        }
        return;
    }

    // Mode toggle: backtick ` or Escape
    if (key == '`' || key == 27) {
        s_dump_mode = !s_dump_mode;
        strncpy(s_status, s_dump_mode ? "MODE:DUMP" : "MODE:ASK", 40);
        refresh_hud_and_input();
        return;
    }

    // Printable ASCII
    if (key >= 0x20 && key < 0x7F) {
        if (s_input_len < INPUT_MAX) {
            s_input[s_input_len++] = (char)key;
            refresh_hud_and_input();
        }
        return;
    }
}

// ════════════════════════════════════════════════════════════════════════════
//  MIC SPIKE — compile-gated behind ENABLE_MIC
//  DO NOT remove; keep stubs so the keyboard path is never broken.
//  TODO: integrate real PTT key, serial audio chunks, and Whisper pipeline
//        on the Ubuntu side before enabling.
// ════════════════════════════════════════════════════════════════════════════

#if ENABLE_MIC

#include <SD.h>  // TODO: confirm SD pin mapping for Cardputer (CLK=40, DATA0=39, CMD=14, CS=12)

static constexpr size_t MIC_RATE  = 16000;  // Hz — Whisper expects 16000
static constexpr size_t MIC_CHUNK = 256;    // samples per record() call
static int16_t s_mic_buf[MIC_CHUNK];
static bool    s_recording = false;
static File    s_sd_file;
static int     s_audio_seq = 0;

// TODO: call once from setup() after Speaker.end() if mic is enabled
static void micBegin() {
    M5Cardputer.Speaker.end();  // REQUIRED: mic and speaker are mutually exclusive
    // Optionally override pin config for v1.1 variant:
    // auto micCfg = M5Cardputer.Mic.config();
    // micCfg.pin_data_in = 46;
    // micCfg.pin_bck = 43;
    // M5Cardputer.Mic.config(micCfg);
    M5Cardputer.Mic.begin();
}

// TODO: send rec_start JSON, open SD file, set s_recording = true
static void micStart() {
    if (s_recording) return;

    // Send rec_start to host
    Serial.print("{\"cmd\":\"rec_start\"}\n");
    Serial.flush();

    // Open SD file for PCM16 capture (Option A: buffer to SD, then stream)
    // TODO: init SD: SD.begin(12) with CS=GPIO12 if not already inited
    s_sd_file = SD.open("/rec.pcm", FILE_WRITE);
    s_audio_seq = 0;
    s_recording = true;
    strncpy(s_status, "REC...", 40);
    refresh_hud_and_input();
}

// TODO: call in loop() while s_recording == true
static void micCapture() {
    if (!s_recording) return;
    if (!M5Cardputer.Mic.isEnabled()) return;

    // record() fills buf with MIC_CHUNK int16_t samples at MIC_RATE Hz
    M5Cardputer.Mic.record(s_mic_buf, MIC_CHUNK, MIC_RATE);

    if (s_sd_file) {
        // Write raw PCM16 LE to SD
        s_sd_file.write((uint8_t*)s_mic_buf, MIC_CHUNK * sizeof(int16_t));
    }
    // Option B (streaming) would send base64 chunk here instead — see probe notes
}

// TODO: call on PTT key release; closes SD file, sends all chunks then rec_end
static void micEnd() {
    if (!s_recording) return;
    s_recording = false;

    if (s_sd_file) {
        s_sd_file.close();
    }

    // Send rec_end
    Serial.print("{\"cmd\":\"rec_end\"}\n");
    Serial.flush();

    // TODO: re-open file, read in 3072-byte raw chunks, base64-encode each,
    // send as {"cmd":"audio","b64":"<4096 chars>","seq":N,"done":<bool>}\n
    // See probe serial_audio_design for exact framing.
    // For now: just log that rec ended
    strncpy(s_status, "REC END", 40);
    refresh_hud_and_input();
}

#else  // ENABLE_MIC == 0  ──────── STUBS ────────────────────────────────────

// These stubs keep the rest of the code clean without #ifdef everywhere
static void micBegin()   { /* MIC disabled — set ENABLE_MIC 1 to activate */ }
static void micCapture() { /* stub */ }
static void micStart()   { /* stub */ }
static void micEnd()     { /* stub */ }

#endif  // ENABLE_MIC

// ════════════════════════════════ SETUP ══════════════════════════════════════

void setup() {
    auto cfg = M5.config();
    M5Cardputer.begin(cfg);

    // USB-CDC serial — physical baud is cosmetic for CDC, but set both ends
    Serial.begin(921600);

    // Display init
    M5Cardputer.Display.setRotation(1);
    M5Cardputer.Display.setBrightness(100);
    M5Cardputer.Display.fillScreen(COL_BG);

    // Sprite (canvas) creation — guard for heap exhaustion
    s_canvas = new M5Canvas(&M5Cardputer.Display);
    if (s_canvas && s_canvas->createSprite(LCD_W, LCD_H)) {
        s_canvas_ok = true;
    } else {
        // Sprite failed (OOM) — fall back to direct draw (will flicker slightly)
        if (s_canvas) {
            delete s_canvas;
            s_canvas = nullptr;
        }
        s_canvas_ok = false;
    }

    // Init scroll buffer
    memset(s_scroll, 0, sizeof(s_scroll));
    memset(s_input,  0, sizeof(s_input));
    s_token_partial_len = 0;
    s_rx_len = 0;

    // Initial scroll message
    scroll_push("Engram ready.", COL_TEXT);
    scroll_push(s_dump_mode ? "Mode: DUMP [`=ask]" : "Mode: ASK  [`=dump]", COL_STATUS);
    scroll_push("!r <s> <txt> -> remind", COL_STATUS);

    // Draw initial frame
    redraw_all();

    // Speaker: brief startup chime so user knows audio works
    M5Cardputer.Speaker.setVolume(64);
    M5Cardputer.Speaker.tone(440, 80);
    delay(90);
    M5Cardputer.Speaker.tone(660, 80);

    // Mic subsystem (no-op when ENABLE_MIC=0)
    micBegin();
}

// ════════════════════════════════ LOOP ═══════════════════════════════════════

void loop() {
    M5Cardputer.update();

    // ── Keyboard ─────────────────────────────────────────────────────────────
    if (M5Cardputer.Keyboard.isChange() && M5Cardputer.Keyboard.isPressed()) {
        Keyboard_Class::KeysState state = M5Cardputer.Keyboard.keysState();

        // TODO (MIC SPIKE): check for PTT key press/release edges here.
        // Example: if (state.fn) { if (!s_recording) micStart(); }
        //          else          { if (s_recording)  micEnd();   }

        for (auto key : state.word) {
            process_key((uint8_t)key);
        }
        if (state.del) {
            process_key('\b');
        }
        if (state.enter) {
            process_key('\r');
        }
    }

    // ── Mic capture (no-op when ENABLE_MIC=0) ────────────────────────────────
    micCapture();

    // ── Serial RX — accumulate until '\n', then dispatch ────────────────────
    while (Serial.available()) {
        int c = Serial.read();
        if (c < 0) break;

        if (c == '\n') {
            s_rx_buf[s_rx_len] = '\0';
            handle_rx_line(s_rx_buf);
            s_rx_len = 0;
        } else {
            if (s_rx_len < RX_BUF - 1) {
                s_rx_buf[s_rx_len++] = (char)c;
            }
            // If buffer overflows, drop oldest: reset and keep accumulating
            // (malformed frame — will fail JSON parse and be ignored)
            else {
                s_rx_len = 0;
            }
        }
    }
}
