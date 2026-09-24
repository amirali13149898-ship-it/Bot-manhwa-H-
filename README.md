# ربات آپلود سنتر تلگرام

## راه‌اندازی

1. **دیتابیس:** تو Supabase برو SQL Editor، محتوای `schema.sql` رو کامل paste کن و Run بزن.
2. **گیت‌هاب:** چهار فایل (`bot.py`, `requirements.txt`, `schema.sql`, `README.md`) رو آپلود کن.
   ⚠️ توکن و کلیدها رو **هیچ‌وقت** تو گیت‌هاب نذار، فقط تو Environment Variables هاست.
3. **هاست (Render):** New → Web Service → ریپو رو وصل کن.
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python bot.py`
   - Plan: Free
4. **Environment Variables:**

| نام | مقدار |
|---|---|
| `BOT_TOKEN` | توکن از @BotFather |
| `OWNER_ID` | آیدی عددی خودت (از @userinfobot) |
| `SUPABASE_URL` | Project URL از Supabase (مثلاً https://xxxx.supabase.co) |
| `SUPABASE_SERVICE_KEY` | کلید service_role |

5. دیپلوی که تموم شد تو ربات `/start` و بعد `/admin` بزن.
6. **بیدار موندن:** پلن رایگان Render بعد از مدتی بی‌درخواستی می‌خوابه. تو UptimeRobot یه مانیتور HTTP روی آدرس سرویس (هر ۵ دقیقه) بذار.

## استفاده

- **قفل‌ها:** ربات رو ادمین کانال/گروه کن → 🔒 قفل ها → ➕ افزودن → یوزرنیم یا یه پیام فوروارد شده از کانال → اجباری/اختیاری.
- **آپلود:** 📤 آپلود فایل → تکی (هر فایل یه لینک) یا گروهی (چند فایل، یه لینک).
- **لینک:** `https://t.me/YourBot?start=CODE`؛ کاربر باید تو قفل‌های اجباری عضو باشه تا فایل بگیره.
- **متن استارت / کپشن پیشفرض / ادمین‌ها:** ⚙️ تنظیمات (مدیریت ادمین‌ها فقط برای OWNER_ID).

## اجرای لوکال (تست)

```
pip install -r requirements.txt
export BOT_TOKEN=... OWNER_ID=... SUPABASE_URL=... SUPABASE_SERVICE_KEY=...
python bot.py
```
(بدون `RENDER_EXTERNAL_URL` خودش با polling اجرا می‌شه.)
