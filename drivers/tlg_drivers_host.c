/* Drivers on a PC: print what the hardware would do. Inputs read as off. */
#include "tlg_drivers.h"

#include <stdio.h>
#include <time.h>

static time_t started;

void    tlg_led_init(const tlg_led_t *d)          { (void)d; if (!started) started = time(NULL); }
void    tlg_led_on(const tlg_led_t *d)            { printf("    [led %d] on\n", d->pin); }
void    tlg_led_off(const tlg_led_t *d)           { printf("    [led %d] off\n", d->pin); }
void    tlg_led_blink(const tlg_led_t *d, int32_t times, int32_t ms)
{
    printf("    [led %d] blink %ldx %ld ms\n", d->pin, (long)times, (long)ms);
}

void    tlg_buzzer_init(const tlg_buzzer_t *d)    { (void)d; }
void    tlg_buzzer_beep(const tlg_buzzer_t *d, int32_t ms) { printf("    [buzzer %d] beep %ld ms\n", d->pin, (long)ms); }
void    tlg_buzzer_tone(const tlg_buzzer_t *d, int32_t hz, int32_t ms)
{
    printf("    [buzzer %d] tone %ld Hz %ld ms\n", d->pin, (long)hz, (long)ms);
}

void    tlg_servo_init(const tlg_servo_t *d)      { (void)d; }
void    tlg_servo_turn(const tlg_servo_t *d, int32_t angle) { printf("    [servo %d] turn to %ld\n", d->pin, (long)angle); }

void    tlg_input_init(const tlg_input_t *d)      { (void)d; }
bool    tlg_input_read(const tlg_input_t *d)      { (void)d; return false; }
bool    tlg_input_pressed(const tlg_input_t *d)   { (void)d; return false; }

void    tlg_analog_init(const tlg_analog_t *d)    { (void)d; }
int32_t tlg_analog_read(const tlg_analog_t *d)    { (void)d; return 0; }

void    tlg_camera_init(const tlg_camera_t *d)    { (void)d; }
bool    tlg_camera_snapshot(const tlg_camera_t *d) { (void)d; printf("    [camera] snapshot\n"); return true; }
bool    tlg_camera_last(const uint8_t **jpeg, size_t *len) { *jpeg = NULL; *len = 0; return false; }

static int32_t fake_hour = -1, fake_minutes = -1;    /* set by tlg_host_clock (tests) */

void tlg_host_clock(int32_t hour, int32_t minutes) { fake_hour = hour; fake_minutes = minutes; }

int32_t tlg_clock_hour(void)
{
    if (fake_hour >= 0) return fake_hour;
    time_t t = time(NULL);
    return (int32_t)localtime(&t)->tm_hour;
}

int32_t tlg_clock_minutes(void)
{
    if (fake_minutes >= 0) return fake_minutes;
    if (!started) started = time(NULL);
    return (int32_t)((time(NULL) - started) / 60);
}

bool    tlg_wifi_connected(void)                  { return true; }

#ifndef TLG_HOST_NO_PHONE
void    tlg_phone_send(const char *text)          { printf("    [phone] %s\n", text); }
#endif
