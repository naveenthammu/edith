import speech_recognition as sr
import pyttsx3
import datetime
import pyautogui as a
import webbrowser

engine = pyttsx3.init()
def speak(audio):
    engine.say(audio)
    engine.runAndWait()
speak("this is edith 2 point o at your service sir")
speak("what is your name sir")
def listing():

    r = sr.Recognizer()
    with sr.Microphone() as source:
        print("Listening...")
        audio = r.listen(source)
        text = r.recognize_google(audio)
        print(text)
        speak("recognized voice"+ text)
        if "Naveen" in text:
            speak("welcome back mister naveen i am at your service sir")
        else:
            speak("this edith at your service sir ")
listing()
def wishMe():
    hour = int(datetime.datetime.now().hour)
    if hour>= 0 and hour<=12:
        speak("Good Morning Sir and have a nice day sir!")
    elif hour>= 12 and hour<16:
        speak("Good Afternoon sir have a great day sir!")
    elif hour>=17 and hour<19:
        speak("good Evening sir!")
    else:
        speak(" i wish your day is going good sir!")

wishMe()
def take_command():
    r = sr.Recognizer()
    with sr.Microphone() as source:
        speak("what do you like to do sir")
        print("Listening...")
        audio = r.listen(source)
        text = r.recognize_google(audio)
        speak("recognize voice"+text)
        print(text)
        if "tell me a joke" in text:
            speak("hell is sweeter than licking some bitchs pussy")
        if "tell about yourself" in text:
            speak("this is edith 2 point o and this is AI assistant  ")
            speak("how can help you sir")
        if "even or odd" in text:
            num = text.split()
            for i in num:
                if i.isdigit():
                    x = int(i)
                    if x % 2 == 0:
                        speak(x)
                        speak("is a even number")
                    else:
                        speak(x)
                        speak("is a odd number")

        if "spell" in text:
            word = text.split()
            for i in range(len(word)):
                if word == "spell":
                    word[i+1]
                    for j in word:
                        speak(j)
        if "open browser" in text:
            speak("opening chrome sir please give me a movement sir!")
            url1 = 'youtube.com'
            url2 = "google.com"
            url3 = "github.com"
            chrome_path = r'/Applications/Google Chrome.app'
            webbrowser.register('chrome', None, webbrowser.BackgroundBrowser(chrome_path))
            webbrowser.get('chrome').open_new_tab(url1)
            webbrowser.get('chrome').open_new_tab(url2)
            webbrowser.get('chrome').open_new_tab(url3)
        if "open YouTube" in text:
            speak("opening the website sir give me a movement sir!")
            a.click(201, 86)
            a.typewrite("https://www.youtube.com/watch?v=NeXbmEnpSz0&list=RDNeXbmEnpSz0&start_radio=1")
            a.typewrite(["enter"])
        if "open whats app" in text:
            speak("opening whatsapp web please give me a movement sir")
            a.click(201, 86)
            a.typewrite("https://web.whatsapp.com/")
            a.typewrite(["enter"])
        if "open Instagram" in text:
            a.click(201, 86)
            speak("opening instagram on website please give me a movement sir!")
            a.typewrite("https://www.instagram.com/")
            a.typewrite(["enter"])
        if "open mail" in text:
            a.click(201, 86)
            speak("opening the gmail please give me a movement sir !")
            a.typewrite("https://mail.google.com/")
            a.typewrite(["enter"])
take_command()
