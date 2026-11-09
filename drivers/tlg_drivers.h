/*
 * Built-in drivers for tiny-langgraph agents. The Python classes in
 * python/tiny_langgraph/drivers.py call these on the board.
 *
 *   tlg_drivers_host.c       on a PC: prints what would happen
 *   tlg_drivers_arduino.cpp  on ESP32 and ESP8266 boards with the Arduino core
 *
 * For another board, implement these functions for it.
 */
#ifndef TLG_DRIVERS_H
#define TLG_DRIVERS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct { int pin; bool active_low; } tlg_led_t;
void    tlg_led_init(const tlg_led_t *d);
void    tlg_led_on(const tlg_led_t *d);
void    tlg_led_off(const tlg_led_t *d);
void    tlg_led_blink(const tlg_led_t *d, int32_t times, int32_t ms);

typedef struct { int pin; } tlg_buzzer_t;
void    tlg_buzzer_init(const tlg_buzzer_t *d);
void    tlg_buzzer_beep(const tlg_buzzer_t *d, int32_t ms);
void    tlg_buzzer_tone(const tlg_buzzer_t *d, int32_t hz, int32_t ms);

typedef struct { int pin; } tlg_servo_t;
void    tlg_servo_init(const tlg_servo_t *d);
void    tlg_servo_turn(const tlg_servo_t *d, int32_t angle);        /* 0..180 degrees */

typedef struct { int pin; bool pullup; bool active_low; bool pulldown; } tlg_input_t;   /* pulldown: unconnected reads LOW */
void    tlg_input_init(const tlg_input_t *d);
bool    tlg_input_read(const tlg_input_t *d);
bool    tlg_input_pressed(const tlg_input_t *d);

typedef struct { int pin; } tlg_analog_t;
void    tlg_analog_init(const tlg_analog_t *d);
int32_t tlg_analog_read(const tlg_analog_t *d);                     /* 0..4095 */

typedef struct { int model; } tlg_camera_t;                          /* 0 esp32s3_eye, 1 xiao_esp32s3 */
void    tlg_camera_init(const tlg_camera_t *d);
bool    tlg_camera_snapshot(const tlg_camera_t *d);
/* The last snapshot as JPEG (board side, for serving it). Returns false if there is none. */
bool    tlg_camera_last(const uint8_t **jpeg, size_t *len);

int32_t tlg_clock_hour(void);       /* 0..23: real time once synced, else counted from a start hour */
int32_t tlg_clock_minutes(void);    /* minutes since the board started */
void    tlg_host_clock(int32_t hour, int32_t minutes);   /* PC only (tests): pretend it's this time; -1 = real */

bool    tlg_wifi_connected(void);

/* A message for the owner. The board's main file implements it (web page, serial). */
void    tlg_phone_send(const char *text);

#ifdef __cplusplus
}
#endif

#endif /* TLG_DRIVERS_H */
