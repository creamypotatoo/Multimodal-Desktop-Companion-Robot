from audio.speech_to_text import record_audio, transcribe
from audio.text_to_speech import speak
from behavior.state_machine import RobotStateMachine, State

def generate_response(user_text):
    text = user_text.lower()

    if "hello" in text or "hi" in text:
        return "Hello! I am Eli, nice to hear you."
    elif "how are you" in text:
        return "I am just a bunch of code right now, but I am doing great."
    elif "name" in text:
        return "My name is Eli."
    elif "bye" in text:
        return "Goodbye! Talk to you soon."
    else:
        return "I heard you, but I am not sure how to respond to that yet."

def conversation_turn(fsm):
    """
    One full listen -> think -> speak cycle.
    Designed to be run in a background thread so it doesn't block video.
    """
    fsm.transition_to(State.LISTENING)
    audio_file = record_audio()
    user_text = transcribe(audio_file)
    print("You said:", user_text)

    if user_text.strip():
        fsm.transition_to(State.THINKING)
        response = generate_response(user_text)

        fsm.transition_to(State.SPEAKING)
        print("Eli says:", response)
        speak(response)

    fsm.transition_to(State.IDLE)

def run_conversation_loop():
    fsm = RobotStateMachine()
    print("Eli is listening. Press Ctrl+C to stop.")

    try:
        while True:
            fsm.transition_to(State.LISTENING)
            audio_file = record_audio()
            user_text = transcribe(audio_file)
            print("You said:", user_text)

            if user_text.strip() == "":
                fsm.transition_to(State.IDLE)
                print("Didn't catch that, try again.")
                continue

            fsm.transition_to(State.THINKING)
            response = generate_response(user_text)

            fsm.transition_to(State.SPEAKING)
            print("Eli says:", response)
            speak(response)

            fsm.transition_to(State.IDLE)

    except KeyboardInterrupt:
        fsm.transition_to(State.IDLE)
        print("\nConversation ended.")

if __name__ == "__main__":
    run_conversation_loop()