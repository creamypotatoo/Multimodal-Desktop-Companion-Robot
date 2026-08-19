class MockMotors:
    """
    A fake motor controller.
    Prints what it WOULD do instead of actually moving anything.
    Later, RealMotors will have these exact same method names,
    but will send commands to the ESP32 instead of printing.
    """

    def move_forward(self):
        print("[SIMULATION] Moving forward")

    def move_backward(self):
        print("[SIMULATION] Moving backward")

    def turn_left(self):
        print("[SIMULATION] Turning left")

    def turn_right(self):
        print("[SIMULATION] Turning right")

    def stop(self):
        print("[SIMULATION] Stopping")