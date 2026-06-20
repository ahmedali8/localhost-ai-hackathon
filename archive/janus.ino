/*
 * janus.ino  —  Cardputer firmware for Project Janus
 *
 * Janus is a CONTRADICTION CATCHER — it argues with your past self.
 * Log claims/decisions (LOG mode); query your contradiction history (ASK mode).
 * The agent detects when a new entry contradicts a prior one and surfaces it here.
 *
 * Hardware : M5Cardputer (ESP32-S3, 240x135 LCD, 56-key kbd, SPM1423 mic)
 * Protocol : NDJSON over USB-CDC  (ARDUINO_USB_CDC_ON_BOOT=1 required)
 * Libs     : M5Cardputer, ArduinoJson v7
 *
 * Build flags (platformio.ini):
 *   build_flags = -D ARDUINO_USB_CDC_ON_BOOT=1
 *
 * Wire protocol (Cardputer -> laptop):
 *   {"cmd":"dump","text":"..."}       // LOG mode — log a claim/decision
 *   {"cmd":"ask","text":"..."}        // ASK mode — query the brain
 *   {"cmd":"remind","text":"...","in_s":<int>}
 *   {"cmd":"rec_start"} / {"cmd":"rec_end"}     // MIC SPIKE only
 *   {"cmd":"audio","b64":"...","seq":N,"done":<bool>}  // MIC SPIKE only
 *
 * Wire protocol (laptop -> Cardputer):
 *   {"status":"CHECKING|CONFLICT!|CONSISTENT|..."}
 *   {"t":"<token>"}                   // streamed answer token
 *   {"e":1}                           // end of stream
 *   {"link":"CONTRADICTS: ..."}       // shown RED  — prior claim was contradicted
 *   {"link":"ECHOES: ..."}            // shown CYAN — prior claim is echoed/aligned
 *   {"notify":"<reminder text>"}      // beep + show
 *   {"err":"<msg>"}
 */

// ─────────────────────────────────────────────────────────────────────────────
// MIC SPIKE — gated behind this define. Set to 1 to compile mic code.
// WARNING: keep at 0 until keyboard path is verified stable.
// The keyboard-only LOG/ASK pipeline MUST work regardless of this flag.
#define ENABLE_MIC 0
// ─────────────────────────────────────────────────────────────────────────────

#include <M5Cardputer.h>
#include <ArduinoJson.h>

// ═════════════════════════════ DISPLAY CONSTANTS ════════════════════════════

static constexpr int LCD_W       = 240;
static constexpr int LCD_H       = 135;
static constexpr int FONT_W      = 6;   // font 0 glyph width  (pixels)
static constexpr int FONT_H      = 8;   // font 0 glyph height (pixels)
static constexpr int COLS        = 40;  // max chars per scroll line
static constexpr int HUD_H       = 10;  // HUD bar height (pixels)
static constexpr int INPUT_H     = 10;  // input bar height (pixels)
static constexpr int SCROLL_Y    = HUD_H;
static constexpr int SCROLL_H    = LCD_H - HUD_H - INPUT_H;
static constexpr int SCROLL_ROWS = SCROLL_H / FONT_H;  // 14 visible rows

// Colours (RGB565)
static constexpr uint16_t COL_BG           = TFT_BLACK;
static constexpr uint16_t COL_HUD_BG_NORM  = 0x0010;   // dark navy (normal)
static constexpr uint16_t COL_HUD_BG_CONF  = 0x6000;   // dark red  (CONFLICT!)
static constexpr uint16_t COL_HUD_FG       = TFT_WHITE;
static constexpr uint16_t COL_TEXT         = TFT_WHITE;
static constexpr uint16_t COL_INPUT_FG     = TFT_GREEN;
static constexpr uint16_t COL_CURSOR       = TFT_GREEN;
static constexpr uint16_t COL_LINK_CONTRA  = TFT_RED;   // "CONTRADICTS: ..."
static constexpr uint16_t COL_LINK_ECHOES  = TFT_CYAN;  // "ECHOES: ..."
static constexpr uint16_t COL_LINK_OTHER   = TFT_YELLOW; // any other link value
static constexpr uint16_t COL_NOTIFY       = TFT_MAGENTA;
static constexpr uint16_t COL_ERR          = TFT_RED;
static constexpr uint16_t COL_MODE_LOG     = TFT_ORANGE; // LOG mode badge
static constexpr uint16_t COL_MODE_ASK     = TFT_CYAN;   // ASK mode badge
static constexpr uint16_t COL_ECHO_LOG     = TFT_ORANGE; // echo of LOG input
static constexpr uint16_t COL_ECHO_ASK     = TFT_CYAN;   // echo of ASK input

// ════════════════════════════════ STATE ═════════════════════════════════════

// ── Scroll ring buffer ───────────────────────────────────────────────────────
struct ScrollLine {
    char     text[COLS + 1];
    uint16_t color;
};
static ScrollLine s_scroll[SCROLL_ROWS];
static int s_scroll_head  = 0;  // next write slot (ring)
static int s_scroll_count = 0;  // filled slots (0..SCROLL_ROWS)

// ── Input buffer ─────────────────────────────────────────────────────────────
static constexpr int INPUT_MAX = 160;
static char s_input[INPUT_MAX + 1];
static int  s_input_len = 0;

// ── HUD state ────────────────────────────────────────────────────────────────
// Status string shown in the HUD (≤40 chars).  Three canonical values from spec:
//   "CONSISTENT"   — no contradiction found
//   "CHECKING"     — agent is processing
//   "CONFLICT!"    — contradiction detected → HUD background turns RED
static char s_status[41] = "CONSISTENT";
static bool s_conflict   = false;  // true when s_status == "CONFLICT!"

// ── Mode: LOG = log a claim/decision; ASK = query the brain ─────────────────
// Toggled with ` (backtick) or Escape.
// Default: LOG mode (primary use case is logging claims).
static bool s_log_mode = true;  // true = LOG, false = ASK

// ── Token accumulation (streaming LLM answer) ───────────────────────────────
static char s_token_partial[COLS + 1];
static int  s_token_partial_len = 0;

// ── Serial RX line buffer ───────────────────────────────────────────────────
static constexpr int RX_BUF = 512;
static char s_rx_buf[RX_BUF];
static int  s_rx_len = 0;

// ── Sprite (double-buffer canvas) ───────────────────────────────────────────
static M5Canvas* s_canvas    = nullptr;
static bool      s_canvas_ok = false;

// ═══════════════════════════════ SCROLL API ══════════════════════════════════

// Push one line (truncated to COLS) into the ring.
static void scroll_push(const char* text, uint16_t color) {
    ScrollLine& slot = s_scroll[s_scroll_head];
    strncpy(slot.text, text, COLS);
    slot.text[COLS] = '\0';
    slot.color = color;
    s_scroll_head = (s_scroll_head + 1) % SCROLL_ROWS;
    if (s_scroll_count < SCROLL_ROWS) s_scroll_count++;
}

// Push text with automatic 40-char hard-wrap (no word-wrap — simple chunking).
static void scroll_push_wrapped(const char* text, uint16_t color) {
    int len = (int)strlen(text);
    if (len == 0) {
        scroll_push("", color);
        return;
    }
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
}

// Append a streamed token to the in-progress partial line; flush when full.
static void token_append(const char* tok) {
    int tlen = (int)strlen(tok);
    for (int i = 0; i < tlen; i++) {
        char c = tok[i];
        if (c == '\n' || s_token_partial_len >= COLS) {
            // Flush current partial line
            s_token_partial[s_token_partial_len] = '\0';
            scroll_push(s_token_partial, COL_TEXT);
            s_token_partial_len = 0;
            if (c == '\n') continue;
        }
        if (c >= 0x20) {  // printable ASCII only
            s_token_partial[s_token_partial_len++] = c;
        }
    }
}

// Flush any remaining partial line (called on end-of-stream {"e":1}).
static void token_flush() {
    if (s_token_partial_len > 0) {
        s_token_partial[s_token_partial_len] = '\0';
        scroll_push(s_token_partial, COL_TEXT);
        s_token_partial_len = 0;
    }
}

// ═══════════════════════════════ RENDERING ═══════════════════════════════════

// Return the active draw target: canvas (double-buffered) or raw display.
static LovyanGFX* draw_target() {
    return s_canvas_ok ? (LovyanGFX*)s_canvas
                       : (LovyanGFX*)&M5Cardputer.Display;
}

static void draw_hud() {
    LovyanGFX* dst = draw_target();

    // Background: dark red on CONFLICT!, dark navy otherwise
    uint16_t bg = s_conflict ? COL_HUD_BG_CONF : COL_HUD_BG_NORM;
    dst->fillRect(0, 0, LCD_W, HUD_H, bg);

    dst->setFont(&fonts::Font0);
    dst->setTextSize(1);

    // ── Mode badge: [L] orange (LOG) / [A] cyan (ASK) ────────────────────────
    uint16_t mode_col  = s_log_mode ? COL_MODE_LOG : COL_MODE_ASK;
    const char* mode_s = s_log_mode ? "[L]"        : "[A]";
    dst->setTextColor(mode_col);
    dst->setCursor(1, 1);
    dst->print(mode_s);

    // ── Status (centre, up to ~26 chars before title) ─────────────────────────
    dst->setTextColor(COL_HUD_FG);
    dst->setCursor(22, 1);
    char trunc[27];
    strncpy(trunc, s_status, 26);
    trunc[26] = '\0';
    dst->print(trunc);

    // ── Title "JANUS" on far right (5 chars * 6px = 30px) ────────────────────
    // Use bright white normally; bright red on conflict for maximum contrast on
    // the dark-red background.
    uint16_t title_col = s_conflict ? TFT_WHITE : TFT_CYAN;
    dst->setTextColor(title_col);
    dst->setCursor(LCD_W - 5 * FONT_W - 1, 1);
    dst->print("JANUS");
}

static void draw_scroll() {
    LovyanGFX* dst = draw_target();
    dst->fillRect(0, SCROLL_Y, LCD_W, SCROLL_H, COL_BG);

    int total = s_scroll_count < SCROLL_ROWS ? s_scroll_count : SCROLL_ROWS;
    for (int row = 0; row < total; row++) {
        // Map display row (0 = oldest visible) to ring index
        int idx = (s_scroll_head - total + row + SCROLL_ROWS) % SCROLL_ROWS;
        int y   = SCROLL_Y + row * FONT_H;
        dst->setFont(&fonts::Font0);
        dst->setTextSize(1);
        dst->setTextColor(s_scroll[idx].color);
        dst->setCursor(0, y);
        dst->print(s_scroll[idx].text);
    }
}

static void draw_input() {
    LovyanGFX* dst = draw_target();
    int y = LCD_H - INPUT_H;
    dst->fillRect(0, y, LCD_W, INPUT_H, COL_BG);
    dst->setFont(&fonts::Font0);
    dst->setTextSize(1);
    dst->setTextColor(COL_INPUT_FG);
    dst->setCursor(0, y + 1);

    // Show rightmost 39 chars so the cursor stays visible while typing
    int start = (s_input_len > 39) ? s_input_len - 39 : 0;
    for (int i = start; i < s_input_len; i++) {
        dst->print(s_input[i]);
    }
    // Blinking cursor (always shown — no timer, static underscore)
    dst->setTextColor(COL_CURSOR);
    dst->print('_');
}

// Full redraw — use on state changes that affect all three regions.
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

// Partial refresh for HUD + input bar only (typing, mode toggle, status change).
static void refresh_hud_input() {
    draw_hud();
    draw_input();
    if (s_canvas_ok) {
        s_canvas->pushSprite(0, 0);
    }
}

// Partial refresh for scroll area only (new token or link line).
static void refresh_scroll() {
    draw_scroll();
    if (s_canvas_ok) {
        s_canvas->pushSprite(0, 0);
    }
}

// ───────────────────────────────────────────────────────────────────────────
// set_status — update s_status and s_conflict flag together.
// Spec canonical values: "CONSISTENT", "CHECKING", "CONFLICT!"
// ───────────────────────────────────────────────────────────────────────────
static void set_status(const char* st) {
    strncpy(s_status, st, 40);
    s_status[40] = '\0';
    s_conflict = (strcmp(s_status, "CONFLICT!") == 0);
}

// ═══════════════════════════════ TX HELPERS ══════════════════════════════════

// Emit {"cmd":"<cmd>","text":"<escaped>"}\n  — ArduinoJson handles escaping.
static void send_cmd_text(const char* cmd, const char* text) {
    JsonDocument doc;
    doc["cmd"]  = cmd;
    doc["text"] = text;
    char out[INPUT_MAX + 64];
    size_t n = serializeJson(doc, out, sizeof(out));
    out[n] = '\n';
    Serial.write((uint8_t*)out, n + 1);
    Serial.flush();
}

// Emit {"cmd":"remind","text":"<txt>","in_s":<s>}\n
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
    // Strip trailing CR/LF
    int len = (int)strlen(line);
    while (len > 0 && (line[len - 1] == '\r' || line[len - 1] == '\n')) {
        line[--len] = '\0';
    }
    if (len == 0) return;

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, line);
    if (err) return;  // malformed frame — silently discard

    // ── {"status":"CHECKING|CONFLICT!|CONSISTENT|..."} ──────────────────────
    if (doc["status"].is<const char*>()) {
        const char* st = doc["status"].as<const char*>();
        set_status(st);
        refresh_hud_input();
        return;
    }

    // ── {"t":"<token>"} — streamed LLM answer token ─────────────────────────
    if (doc["t"].is<const char*>()) {
        const char* tok = doc["t"].as<const char*>();
        // Hard-cap per-frame to 200 chars as per wire spec
        char capped[201];
        strncpy(capped, tok, 200);
        capped[200] = '\0';
        token_append(capped);
        refresh_scroll();
        return;
    }

    // ── {"e":1} — end of streamed answer ────────────────────────────────────
    if (doc["e"].is<int>() && doc["e"].as<int>() == 1) {
        token_flush();
        // Revert to CONSISTENT after answer stream ends (if not in conflict)
        if (!s_conflict) {
            set_status("CONSISTENT");
        }
        scroll_push("", COL_TEXT);  // blank separator line
        redraw_all();
        return;
    }

    // ── {"link":"CONTRADICTS: ..."} or {"link":"ECHOES: ..."} ────────────────
    // "CONTRADICTS" prefix → RED  (contradiction found)
    // "ECHOES"      prefix → CYAN (claim echoes a prior one)
    // anything else         → YELLOW
    if (doc["link"].is<const char*>()) {
        const char* lnk = doc["link"].as<const char*>();

        uint16_t link_col;
        if (strncmp(lnk, "CONTRADICTS", 11) == 0) {
            link_col = COL_LINK_CONTRA;
            // Also update status to CONFLICT! so HUD turns red
            set_status("CONFLICT!");
        } else if (strncmp(lnk, "ECHOES", 6) == 0) {
            link_col = COL_LINK_ECHOES;
        } else {
            link_col = COL_LINK_OTHER;
        }

        // Show the full link text with a leading marker so it stands out
        char buf[COLS + 1];
        snprintf(buf, sizeof(buf), "%s", lnk);
        scroll_push_wrapped(buf, link_col);
        redraw_all();
        return;
    }

    // ── {"notify":"<reminder text>"} — fire a reminder ──────────────────────
    if (doc["notify"].is<const char*>()) {
        const char* ntf = doc["notify"].as<const char*>();
        char buf[COLS + 1];
        snprintf(buf, sizeof(buf), "!! %s", ntf);
        scroll_push_wrapped(buf, COL_NOTIFY);
        set_status("REMINDER!");
        // Beep: two short tones
        M5Cardputer.Speaker.tone(880, 150);
        delay(180);
        M5Cardputer.Speaker.tone(1100, 150);
        redraw_all();
        return;
    }

    // ── {"err":"<msg>"} ──────────────────────────────────────────────────────
    if (doc["err"].is<const char*>()) {
        const char* emsg = doc["err"].as<const char*>();
        char buf[COLS + 1];
        snprintf(buf, sizeof(buf), "ERR: %s", emsg);
        scroll_push_wrapped(buf, COL_ERR);
        set_status("ERROR");
        redraw_all();
        return;
    }
}

// ════════════════════════════ KEYBOARD HANDLING ══════════════════════════════
/*
 * Key assignments:
 *   printable ASCII   — append to input buffer
 *   Enter / \r        — send current buffer (cmd depends on mode)
 *   Backspace / DEL   — delete last character
 *   ` (backtick)      — toggle LOG / ASK mode
 *   Escape (0x1B)     — also toggles mode (accessible via Fn layer)
 *
 * Remind shorthand (works in either mode, bypasses cmd):
 *   Type:  !r <seconds> <text>   then Enter
 *   Sends: {"cmd":"remind","text":"<text>","in_s":<seconds>}
 *   Example: !r 30 drink water
 */

static void process_enter() {
    if (s_input_len == 0) return;
    s_input[s_input_len] = '\0';

    // ── Remind shorthand: "!r <s> <text>" ───────────────────────────────────
    if (s_input_len > 3 &&
        s_input[0] == '!' && s_input[1] == 'r' && s_input[2] == ' ') {
        char* rest = s_input + 3;
        char* sp   = strchr(rest, ' ');
        if (sp) {
            *sp = '\0';
            int in_s = atoi(rest);
            *sp = ' ';
            char* remind_text = sp + 1;
            if (in_s > 0 && strlen(remind_text) > 0) {
                char echo[COLS + 1];
                snprintf(echo, sizeof(echo), ">RMD %ds: %s", in_s, remind_text);
                scroll_push_wrapped(echo, COL_NOTIFY);
                send_remind(remind_text, in_s);
                set_status("REMIND SET");
                s_input_len = 0;
                redraw_all();
                return;
            }
        }
        // Parse failed — fall through to normal send
    }

    // ── Echo the user's input in the scroll area ─────────────────────────────
    {
        // Prefix: ">L " for LOG, ">A " for ASK
        char echo[INPUT_MAX + 4];
        snprintf(echo, sizeof(echo), "%s%s",
                 s_log_mode ? ">L " : ">A ",
                 s_input);
        scroll_push_wrapped(echo, s_log_mode ? COL_ECHO_LOG : COL_ECHO_ASK);
    }

    // ── Send JSON to host ────────────────────────────────────────────────────
    // LOG mode → cmd "dump" (Janus logs a claim/decision for contradiction check)
    // ASK mode → cmd "ask"  (Janus queries the graph / contradiction history)
    send_cmd_text(s_log_mode ? "dump" : "ask", s_input);

    // Status transitions to CHECKING while the agent works
    set_status("CHECKING");
    s_input_len = 0;
    redraw_all();
}

static void process_key(uint8_t key) {
    // ── Enter ────────────────────────────────────────────────────────────────
    if (key == '\n' || key == '\r') {
        process_enter();
        return;
    }

    // ── Backspace / DEL ──────────────────────────────────────────────────────
    if (key == '\b' || key == 127) {
        if (s_input_len > 0) {
            s_input_len--;
            refresh_hud_input();
        }
        return;
    }

    // ── Mode toggle: backtick or Escape ──────────────────────────────────────
    if (key == '`' || key == 0x1B) {
        s_log_mode = !s_log_mode;
        // Clear input on mode switch to avoid accidental cross-mode sends
        s_input_len = 0;
        set_status("CONSISTENT");
        refresh_hud_input();
        return;
    }

    // ── Printable ASCII ──────────────────────────────────────────────────────
    if (key >= 0x20 && key < 0x7F) {
        if (s_input_len < INPUT_MAX) {
            s_input[s_input_len++] = (char)key;
            refresh_hud_input();
        }
        return;
    }
}

// ════════════════════════════════════════════════════════════════════════════
//  MIC SPIKE — compile-gated behind #define ENABLE_MIC
//
//  Keep this block intact.  The stubs (ENABLE_MIC=0 branch) are called from
//  setup() and loop() unconditionally — the keyboard path NEVER touches #ifdef.
//
//  TODO list for when ENABLE_MIC is set to 1:
//    1. Assign a dedicated PTT key (e.g. Fn key) and track press/release EDGES.
//    2. Call micStart() on key-down, micEnd() on key-up.
//    3. In micEnd(): re-open SD file, base64-encode in 3072-byte chunks,
//       send {"cmd":"audio","b64":"...","seq":N,"done":<bool>}\n per chunk.
//    4. Ubuntu host: receive chunks, concatenate PCM16, run faster-whisper,
//       then feed transcript through the same "dump" pipeline.
//    5. Test: verify VRAM coexistence — evict Ollama (keep_alive:0) before
//       Whisper inference. See probe whisperMic.vram_coexistence.
// ════════════════════════════════════════════════════════════════════════════

#if ENABLE_MIC

#include <SD.h>
// SD pin mapping for M5Cardputer:
//   CLK=GPIO40  DATA0=GPIO39  CMD=GPIO14  CS=GPIO12
// Call SD.begin(12) once in micBegin() before first use.

static constexpr size_t MIC_RATE  = 16000;  // Hz — Whisper trained at 16 kHz
static constexpr size_t MIC_CHUNK = 256;    // samples per Mic.record() call (16 ms)
static int16_t s_mic_buf[MIC_CHUNK];
static bool    s_recording    = false;
static File    s_sd_file;
static int     s_audio_seq    = 0;

// Call once from setup() when mic is enabled.
static void micBegin() {
    // Mic and speaker are mutually exclusive — shut speaker down first.
    M5Cardputer.Speaker.end();

    // Optional: explicit pin override for v1.1 (M5Stamp S3A) variant.
    // auto mc = M5Cardputer.Mic.config();
    // mc.pin_data_in = 46;
    // mc.pin_bck     = 43;
    // M5Cardputer.Mic.config(mc);

    M5Cardputer.Mic.begin();
    SD.begin(12);  // CS = GPIO12
}

// TODO: Call on PTT key-down edge.
static void micStart() {
    if (s_recording) return;
    Serial.print("{\"cmd\":\"rec_start\"}\n");
    Serial.flush();
    s_sd_file  = SD.open("/rec.pcm", FILE_WRITE);
    s_audio_seq = 0;
    s_recording = true;
    set_status("REC...");
    refresh_hud_input();
}

// TODO: Call in loop() while s_recording == true.
// Captures one 256-sample chunk (16 ms) to SD each call.
static void micCapture() {
    if (!s_recording) return;
    if (!M5Cardputer.Mic.isEnabled()) return;
    M5Cardputer.Mic.record(s_mic_buf, MIC_CHUNK, MIC_RATE);
    if (s_sd_file) {
        s_sd_file.write((uint8_t*)s_mic_buf, MIC_CHUNK * sizeof(int16_t));
    }
    // Option B (real-time stream): base64-encode s_mic_buf here and send
    // {"cmd":"audio","b64":"...","seq":N,"done":false}\n instead.
    // See probe whisperMic.serial_audio_design for chunk sizing and flush rules.
}

// TODO: Call on PTT key-up edge.
// Closes SD file, sends rec_end, then re-reads file and streams base64 chunks.
static void micEnd() {
    if (!s_recording) return;
    s_recording = false;
    if (s_sd_file) s_sd_file.close();

    Serial.print("{\"cmd\":\"rec_end\"}\n");
    Serial.flush();

    // TODO: Re-open "/rec.pcm", read in 3072-byte raw chunks, base64-encode
    // to 4096-char strings, send as:
    //   {"cmd":"audio","b64":"<4096>","seq":N,"done":false}\n  ...
    //   {"cmd":"audio","b64":"<last>","seq":N,"done":true}\n
    // Split each JSON frame into <400-char Serial.print() calls + flush to
    // stay within the ESP32-S3 USB-CDC 512-byte tx buffer.
    // (See probe whisperMic.serial_audio_design, gotchas entry 4.)

    set_status("CHECKING");
    refresh_hud_input();
}

#else  // ENABLE_MIC == 0 ── stubs ─────────────────────────────────────────────

static void micBegin()   { /* MIC disabled — set ENABLE_MIC 1 to activate */ }
static void micCapture() { /* stub */ }
static void micStart()   { /* stub */ }
static void micEnd()     { /* stub */ }

#endif  // ENABLE_MIC

// ════════════════════════════════ SETUP ══════════════════════════════════════

void setup() {
    auto cfg = M5.config();
    M5Cardputer.begin(cfg);

    // USB-CDC: physical baud is ignored by the CDC driver but set both ends to
    // the same value to avoid host-side pyserial open errors.
    Serial.begin(921600);

    // Display
    M5Cardputer.Display.setRotation(1);
    M5Cardputer.Display.setBrightness(100);
    M5Cardputer.Display.fillScreen(COL_BG);

    // ── Sprite / canvas — double-buffer to eliminate flicker ─────────────────
    // Guard: if heap is low the createSprite() call returns false; fall back to
    // direct draw (slight flicker on full redraws, but fully functional).
    s_canvas = new M5Canvas(&M5Cardputer.Display);
    if (s_canvas && s_canvas->createSprite(LCD_W, LCD_H)) {
        s_canvas_ok = true;
    } else {
        // OOM or allocation failure — free partial allocation and continue.
        if (s_canvas) {
            delete s_canvas;
            s_canvas = nullptr;
        }
        s_canvas_ok = false;
    }

    // ── Zero all buffers ──────────────────────────────────────────────────────
    memset(s_scroll, 0, sizeof(s_scroll));
    memset(s_input,  0, sizeof(s_input));
    s_token_partial_len = 0;
    s_rx_len            = 0;

    // ── Boot messages in scroll area ──────────────────────────────────────────
    scroll_push("JANUS online.", COL_TEXT);
    scroll_push("Two faces. One truth.", TFT_CYAN);
    scroll_push("`=toggle LOG/ASK mode", TFT_YELLOW);
    scroll_push("!r <s> <msg> -> remind", TFT_YELLOW);
    scroll_push("LOG: claim/decision",    COL_MODE_LOG);
    scroll_push("ASK: query your past",   COL_MODE_ASK);

    // ── Initial draw ──────────────────────────────────────────────────────────
    set_status("CONSISTENT");
    redraw_all();

    // ── Startup chime (two-tone) ──────────────────────────────────────────────
    M5Cardputer.Speaker.setVolume(64);
    M5Cardputer.Speaker.tone(440, 80);
    delay(100);
    M5Cardputer.Speaker.tone(660, 80);
    delay(100);

    // Mic subsystem init (no-op when ENABLE_MIC=0)
    micBegin();
}

// ════════════════════════════════ LOOP ═══════════════════════════════════════

void loop() {
    M5Cardputer.update();

    // ── Keyboard ─────────────────────────────────────────────────────────────
    if (M5Cardputer.Keyboard.isChange() && M5Cardputer.Keyboard.isPressed()) {
        Keyboard_Class::KeysState state = M5Cardputer.Keyboard.keysState();

        // TODO (MIC SPIKE, ENABLE_MIC=1): detect PTT key edge here.
        // Example using Fn key:
        //   static bool was_fn = false;
        //   bool fn_now = state.fn;
        //   if (fn_now && !was_fn) micStart();
        //   if (!fn_now && was_fn) micEnd();
        //   was_fn = fn_now;

        // Process typed characters
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

    // ── Serial RX — accumulate bytes into line buffer, dispatch on '\n' ──────
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
            } else {
                // Buffer overflow — frame is malformed; discard and reset.
                // deserializeJson() will fail gracefully on whatever partial
                // content follows, so this is safe.
                s_rx_len = 0;
            }
        }
    }
}

