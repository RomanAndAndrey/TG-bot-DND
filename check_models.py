import google.generativeai as genai

# Вставьте сюда ваш ключ
GOOGLE_API_KEY = "AIzaSyAglR4zwpSx7g5rzLNUyNJRRrbh1rhSCfc"
genai.configure(api_key=GOOGLE_API_KEY)

print("Список доступных моделей:")
for m in genai.list_models():
    if 'generateContent' in m.supported_generation_methods:
        print(m.name)