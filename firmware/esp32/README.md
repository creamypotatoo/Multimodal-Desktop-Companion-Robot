# Eli ESP32 motor controller

USB-only firmware for ESP32-WROOM-32 DevKit V1 (30-pin) and the TB6612FNG.
Obstacle and cliff detection are unavailable. Current status values describe
software commands. There is no wheel feedback.

## Wiring

Disconnect USB and motor power while wiring. Verify the driver carrier pin
labels and power ratings before connecting the four-AA motor battery supply.

| TB6612FNG pin | Connect to |
|---|---|
| PWMA | ESP32 GPIO25 |
| AIN1 | ESP32 GPIO26 |
| AIN2 | ESP32 GPIO27 |
| PWMB | ESP32 GPIO23 |
| BIN1 | ESP32 GPIO19 |
| BIN2 | ESP32 GPIO18 |
| STBY | ESP32 GPIO13, with an external 10 kÎ© pull-down to GND |
| VCC | ESP32 3.3 V |
| VM | Separate appropriately rated motor battery positive |
| GND | Common ESP32 ground, driver ground and battery negative |
| AO1 | Left motor red |
| AO2 | Left motor black |
| BO1 | Right motor red |
| BO2 | Right motor black |

The ESP32 is powered through USB. Motor A is left, motor B is right.
Both inversion flags default to false. PWM is 20 kHz at 8 bits.
Supply the motors through an accessible physical battery disconnect.
Test initially with both wheels lifted, the chassis secured and the
disconnect within reach. Transition to supervised manual floor movement
only after the bench checks pass. Keep clear of edges and obstacles.

## Build and upload

In Arduino IDE install **esp32 by Espressif Systems** version **3.3.5** from
Boards Manager. Add the Espressif package index URL below in Preferences if
needed. No extra Arduino libraries are required. Select **ESP32 Dev Module**
for the ESP32-WROOM-32 DevKit V1 and select its USB COM port. Open
`eli_motor_controller/eli_motor_controller.ino` from the firmware directory.
Verify first. With motor power disconnected, upload, then open Serial Monitor
at **115200 baud** with **Newline** selected. Opening the port can reset some
boards. Allow boot to finish before querying status.

Arduino CLI equivalent from the firmware worktree root. Replace `COM5` with
the port reported by `arduino-cli board list`.

```powershell
$index = 'https://espressif.github.io/arduino-esp32/package_esp32_index.json'
arduino-cli core update-index --additional-urls $index
arduino-cli core install esp32:esp32@3.3.5 --additional-urls $index
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/esp32/eli_motor_controller
arduino-cli upload --fqbn esp32:esp32:esp32 --port COM5 firmware/esp32/eli_motor_controller
```

## Automated tests

Compile and run the production sketch with fake Arduino pins, serial and
time on Windows using GCC. The second invocation injects a PWM attach failure.

```powershell
$fw = 'firmware/esp32'
$s = "$fw/eli_motor_controller"
$testExe = Join-Path $env:TEMP 'eli-firmware-test.exe'
g++ -D__STDC_FORMAT_MACROS -std=c++14 -Wall -Wextra -Werror -I "$fw/tests" -I $s -x c++ "$s/eli_motor_controller.ino" "$s/motor_driver.cpp" "$s/safety_manager.cpp" "$s/serial_protocol.cpp" "$fw/tests/test_firmware.cpp" -o $testExe
if ($LASTEXITCODE -ne 0) { throw 'Host build failed' }
& $testExe
if ($LASTEXITCODE -ne 0) { throw 'Host test failed' }
& $testExe fail-attach
```

## Bench procedure

1. With motor power disconnected, verify continuity, common ground and the
   STBY pull-down. Reboot and measure STBY low. Send
   `{"id":1,"cmd":"status"}`. Expect stopped, zero duty, and unavailable sensors.
   If healthy, lift both wheels and connect motor power while still stopped.
2. Send `{"id":2,"cmd":"hello"}` and inspect version and session. A session
   expires without a heartbeat. Use a timed host for command sequences rather
   than typing motion commands into Serial Monitor. See [the protocol](PROTOCOL.md).
3. Close Serial Monitor before running the helper. Install PySerial only on the
   bench computer. The default query sends no movement. Each opt-in invocation
   sends one pulse and attempts STOP on exit.

```powershell
python -m pip install pyserial==3.5
python firmware/esp32/tests/bench_serial.py COM5
python firmware/esp32/tests/bench_serial.py COM5 --move forward --wheels-lifted --speed 25 --duration-ms 300
python firmware/esp32/tests/bench_serial.py COM5 --move backward --wheels-lifted --speed 25 --duration-ms 300
python firmware/esp32/tests/bench_serial.py COM5 --move left --wheels-lifted --speed 25 --duration-ms 300
python firmware/esp32/tests/bench_serial.py COM5 --move right --wheels-lifted --speed 25 --duration-ms 300
```

4. Verify left/right rotation against the mapping in the protocol. Compare
   25% and 40% in the helper or measure PWM duty with a scope. Confirm each pulse
   ends by its duration even while the helper is sending heartbeats. Observe
   STBY and the 20 ms disabled interval on a reversal between active pulses.
5. Run the three safety exercises below, one at a time. Observe STBY and the
   wheels during STOP and ESTOP. The helper checks the latch and reset, stale
   session rejection, and that re-arming leaves the motors stopped.
6. During the watchdog exercise, use a scope or logic analyzer to check that
   STBY drops by the 750 ms communication deadline before the pulse's 1,000 ms
   duration expires. Also test loss of USB data without removing ESP32 power
   if the bench setup allows it.
7. Send malformed JSON and a frame with 193 payload characters while moving.
   Verify each stops and invalidates the session. Verify attempts with speed
   0 or 41 and duration 0 or 1,001 ms are rejected. Repeat an accepted move ID
   with a different duration and verify the original deadline is unchanged.
   Queue a move, a STOP and an old-session move together. Verify no restart.
8. Power-cycle with the wheels lifted. Verify no automatic movement and no
   retained serial session before attempting supervised manual floor use.

Safety exercise commands, with both wheels lifted:

```powershell
python firmware/esp32/tests/bench_serial.py COM5 --move forward --wheels-lifted --verify stop
python firmware/esp32/tests/bench_serial.py COM5 --move forward --wheels-lifted --verify estop
python firmware/esp32/tests/bench_serial.py COM5 --move forward --wheels-lifted --verify watchdog
```

## Troubleshooting

- **No serial reply.** Close competing applications, check the USB data cable
  and COM port, and send an LF-terminated request. Ignore boot text.
- **Upload fails.** Check board/port selection. Use the board's BOOT button
  during connection if its USB adapter does not enter bootloader mode.
- **No response to a direction.** Check the status, wiring, motor supply and
  common ground before increasing duty. A small duty may not overcome static
  friction. Retain the configured 40% limit during bench checks.
- **`pwm_fault`.** STBY remains low. Disconnect motor power and inspect the
  build configuration and board before rebooting.
- **`session_invalid`.** Obtain a new session, send a heartbeat, and issue a
  fresh operator action. Replaying a movement frame is unsafe.

## Watchdog scope

The 750 ms communication watchdog runs in the Arduino loop, so it depends
on the processor and loop running. Keep the physical motor power disconnect.
The ESP-IDF Task Watchdog is a candidate for detecting loop hangs. Bench-test
subscribing the Arduino loop task and enabling a reset on timeout for the
selected core configuration. Verify STBY remains low through that reset
with the pull-down installed. This firmware does not configure that watchdog.
[Espressif watchdog documentation](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/system/wdts.html)
