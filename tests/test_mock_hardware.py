from hardware.mock_hardware import MockMotors

def test_motors():
    motors = MockMotors()
    motors.move_forward()
    motors.turn_left()
    motors.stop()

if __name__ == "__main__":
    test_motors()