/*
 * Built-in drivers with the Arduino core, on ESP32 boards (core 2.x and 3.x) and ESP8266 boards
 * (NodeMCU). tlg_build.h (generated) says which optional parts the agent uses; boards.py makes
 * sure the agent only uses what the board has (e.g. pull-downs, analog pins, a camera).
 */
#include <Arduino.h>
#include <time.h>
#if defined(ESP8266)
#include <ESP8266WiFi.h>
#include <Servo.h>
#else
#include <WiFi.h>
#include <esp_arduino_version.h>
#endif

#undef BOOT_PIN                          /* some cores define it; config.h says which pin */
#include "config.h"
#include "tlg_build.h"
#include "tlg_drivers.h"

#if TLG_USE_CAMERA
#include "esp_camera.h"
#endif

/* ---------------- LED ---------------- */
static void led_write(const tlg_led_t *d, bool on) { digitalWrite(d->pin, (on != d->active_low) ? HIGH : LOW); }

void tlg_led_init(const tlg_led_t *d)  { pinMode(d->pin, OUTPUT); led_write(d, false); }
void tlg_led_on(const tlg_led_t *d)    { led_write(d, true); }
void tlg_led_off(const tlg_led_t *d)   { led_write(d, false); }
void tlg_led_blink(const tlg_led_t *d, int32_t times, int32_t ms)
{
    for (int32_t i = 0; i < times; i++) {
        led_write(d, true);  delay(ms);
        led_write(d, false); delay(ms);
    }
}

/* ---------------- buzzer (passive or active) ---------------- */
void tlg_buzzer_init(const tlg_buzzer_t *d) { pinMode(d->pin, OUTPUT); digitalWrite(d->pin, LOW); }
void tlg_buzzer_tone(const tlg_buzzer_t *d, int32_t hz, int32_t ms)
{
    tone(d->pin, (unsigned)hz, (unsigned long)ms);
    delay(ms);
    noTone(d->pin);
}
void tlg_buzzer_beep(const tlg_buzzer_t *d, int32_t ms) { tlg_buzzer_tone(d, 2000, ms); }

/* ---------------- hobby servo: 50 Hz, 0.5-2.5 ms pulses ---------------- */
#if defined(ESP8266)
static Servo servos[4];
static int   servo_pins[4] = { -1, -1, -1, -1 };

static Servo *servo_for(int pin)
{
    for (int i = 0; i < 4; i++) {
        if (servo_pins[i] == pin) return &servos[i];
        if (servo_pins[i] < 0) { servo_pins[i] = pin; return &servos[i]; }
    }
    return &servos[3];
}

void tlg_servo_init(const tlg_servo_t *d) { servo_for(d->pin)->attach(d->pin, 500, 2500); }

void tlg_servo_turn(const tlg_servo_t *d, int32_t angle)
{
    if (angle < 0) angle = 0;
    if (angle > 180) angle = 180;
    servo_for(d->pin)->write((int)angle);
    delay(400);                 /* give it time to get there */
}
#else
#if ESP_ARDUINO_VERSION_MAJOR < 3                 /* core 3 attaches by pin, no channels */
static int servo_channel(int pin)
{
    static int pins[4] = { -1, -1, -1, -1 };
    for (int i = 0; i < 4; i++) {
        if (pins[i] == pin) return 4 + i;
        if (pins[i] < 0) { pins[i] = pin; return 4 + i; }
    }
    return 7;
}
#endif

void tlg_servo_init(const tlg_servo_t *d)
{
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(d->pin, 50, 14);
#else
    int ch = servo_channel(d->pin);
    ledcSetup(ch, 50, 14);
    ledcAttachPin(d->pin, ch);
#endif
}

void tlg_servo_turn(const tlg_servo_t *d, int32_t angle)
{
    if (angle < 0) angle = 0;
    if (angle > 180) angle = 180;
    uint32_t us = 500 + (uint32_t)angle * 2000 / 180;
    uint32_t duty = us * 16383 / 20000;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(d->pin, duty);
#else
    ledcWrite(servo_channel(d->pin), duty);
#endif
    delay(400);                 /* give it time to get there */
}
#endif

/* ---------------- inputs ---------------- */
#if defined(ESP8266)
/* only GPIO16 (D0) has a pull-down on the ESP8266; the build refuses pulldown=True elsewhere */
void tlg_input_init(const tlg_input_t *d)
{
    pinMode(d->pin, d->pullup ? INPUT_PULLUP : (d->pulldown && d->pin == 16) ? INPUT_PULLDOWN_16 : INPUT);
}
#else
void tlg_input_init(const tlg_input_t *d)    { pinMode(d->pin, d->pullup ? INPUT_PULLUP : d->pulldown ? INPUT_PULLDOWN : INPUT); }
#endif
bool tlg_input_read(const tlg_input_t *d)    { return (digitalRead(d->pin) == HIGH) != d->active_low; }
bool tlg_input_pressed(const tlg_input_t *d) { return tlg_input_read(d); }

void    tlg_analog_init(const tlg_analog_t *d) { (void)d; }
#if defined(ESP8266)
int32_t tlg_analog_read(const tlg_analog_t *d) { (void)d; return analogRead(A0) * 4; }   /* 0..1023 -> 0..4092 */
#else
int32_t tlg_analog_read(const tlg_analog_t *d) { return analogRead(d->pin); }
#endif

/* ---------------- clock ---------------- */
int32_t tlg_clock_hour(void)
{
    time_t now = time(NULL);
    if (now > 1600000000) {                    /* synced from the internet */
        struct tm t;
        localtime_r(&now, &t);
        return t.tm_hour;
    }
    return (int32_t)((START_HOUR + millis() / 3600000UL) % 24);
}

int32_t tlg_clock_minutes(void) { return (int32_t)(millis() / 60000UL); }

bool tlg_wifi_connected(void) { return WiFi.status() == WL_CONNECTED; }

/* ---------------- camera ---------------- */
#if TLG_USE_CAMERA
static uint8_t          *last_jpeg;
static size_t            last_len, last_cap;
static SemaphoreHandle_t cam_lock;

void tlg_camera_init(const tlg_camera_t *d)
{
    /* pin maps from the Arduino core's CameraWebServer example */
    static const int8_t EYE[]  = { -1, -1, 15, 4, 5, 16, 17, 18, 12, 10, 8, 9, 11, 6, 7, 13 };
    static const int8_t XIAO[] = { -1, -1, 10, 40, 39, 48, 11, 12, 14, 16, 18, 17, 15, 38, 47, 13 };
    const int8_t *p = d->model == 1 ? XIAO : EYE;
    camera_config_t c = {};
    c.pin_pwdn = p[0];  c.pin_reset = p[1];  c.pin_xclk = p[2];
    c.pin_sccb_sda = p[3];  c.pin_sccb_scl = p[4];
    c.pin_d7 = p[5];  c.pin_d6 = p[6];  c.pin_d5 = p[7];  c.pin_d4 = p[8];
    c.pin_d3 = p[9];  c.pin_d2 = p[10]; c.pin_d1 = p[11]; c.pin_d0 = p[12];
    c.pin_vsync = p[13];  c.pin_href = p[14];  c.pin_pclk = p[15];
    c.xclk_freq_hz = 20000000;
    c.ledc_timer = LEDC_TIMER_1;
    c.ledc_channel = LEDC_CHANNEL_1;
    c.pixel_format = PIXFORMAT_JPEG;
    c.frame_size = FRAMESIZE_VGA;
    c.jpeg_quality = 12;
    c.fb_count = 1;
    c.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
    c.grab_mode = CAMERA_GRAB_LATEST;
    cam_lock = xSemaphoreCreateMutex();
    esp_err_t err = esp_camera_init(&c);
    Serial.printf("camera: %s\n", err == ESP_OK ? "ready" : "NOT found (check Camera(model=...))");
}

bool tlg_camera_snapshot(const tlg_camera_t *d)
{
    (void)d;
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) return false;
    bool ok = false;
    xSemaphoreTake(cam_lock, portMAX_DELAY);
    if (fb->len > last_cap) {
        free(last_jpeg);
        last_cap = fb->len + 4096;
        last_jpeg = (uint8_t *)(psramFound() ? ps_malloc(last_cap) : malloc(last_cap));
        if (!last_jpeg) last_cap = 0;
    }
    if (last_jpeg) {
        memcpy(last_jpeg, fb->buf, fb->len);
        last_len = fb->len;
        ok = true;
    }
    xSemaphoreGive(cam_lock);
    esp_camera_fb_return(fb);
    return ok;
}

/* The caller copies or sends it right away; a new snapshot may replace it later. */
bool tlg_camera_last(const uint8_t **jpeg, size_t *len)
{
    if (!cam_lock) return false;
    xSemaphoreTake(cam_lock, portMAX_DELAY);
    *jpeg = last_jpeg;
    *len = last_len;
    xSemaphoreGive(cam_lock);
    return last_len > 0;
}
#else
void tlg_camera_init(const tlg_camera_t *d) { (void)d; }
bool tlg_camera_snapshot(const tlg_camera_t *d) { (void)d; return false; }
bool tlg_camera_last(const uint8_t **jpeg, size_t *len) { *jpeg = NULL; *len = 0; return false; }
#endif
