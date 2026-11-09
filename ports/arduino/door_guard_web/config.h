/* Settings for the door guard board. Edit these, then upload. */
#pragma once

/* Your Wi-Fi. Leave WIFI_SSID empty to always use the board's own hotspot. */
#define WIFI_SSID        ""
#define WIFI_PASSWORD    ""

/* If your Wi-Fi can't be reached, the board starts its own network instead:
   join it, then open http://192.168.4.1  (password: at least 8 characters) */
#define AP_PASSWORD      "doorguard"

/* The web page is at http://door-guard.local (or the IP printed at startup). */
#define HOSTNAME         "door-guard"

/* PIN for approving risky actions from the web page. Without it the page is
   read-only. Change it: anyone on your network can open the page. */
#define APPROVAL_PIN     "4721"

/* How long the agent waits for an answer before treating it as "no". */
#define APPROVAL_TIMEOUT_MS  120000

/* Run the next demo scenario every N ms (0 = only on request: type 1-8 in
   the Serial Monitor). */
#define DEMO_INTERVAL_MS 20000

/* 1: the Serial Monitor shows @tlg trace lines (for the viewer over USB).
   0: it shows readable text instead. The web page works either way. */
#define SERIAL_TRACE     0

/* An LED to blink for led_flash / alarm, or -1 for none. */
#define LED_PIN          -1

/* BOOT button: pressing it approves a pending request at the device. */
#define BOOT_PIN         0
