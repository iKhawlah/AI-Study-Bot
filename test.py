import os
from dotenv import load_dotenv
from google import genai

# تحميل المفاتيح من ملف .env
load_dotenv()

api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    print("❌ الخطأ الأول: البايثون مو قادر يلقى مفتاح GEMINI_API_KEY. تأكد من اسم ملف الـ .env")
else:
    print("✅ تم العثور على المفتاح بنجاح!")
    
    try:
        print("جاري محاولة الاتصال بالذكاء الاصطناعي...")
        # نمرر المفتاح بشكل مباشر عشان نضمن أنه يوصل
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents="مرحبا، هل أنت متصل؟"
        )
        print("\n✅ الاتصال شغال 100%! هذا رد الذكاء الاصطناعي:")
        print(response.text)
    except Exception as e:
        print("\n❌ فشل الاتصال، وهذا هو السبب الحقيقي للخطأ:")
        print(e)