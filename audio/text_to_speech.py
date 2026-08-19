import pyttsx3

def speak(text):
    engine = pyttsx3.init()

    # Optional: adjust speaking rate (default is usually ~200)
    engine.setProperty('rate', 175)

    engine.say(text)
    engine.runAndWait()  # blocks until speech finishes

if __name__ == "__main__":
    speak("Hello, I am Eli. Nice to meet you.")