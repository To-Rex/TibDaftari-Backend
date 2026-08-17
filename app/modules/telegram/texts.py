"""Bot texts — the four NavbatApp languages (uz, uzc, ru, en), adapted to orders / payments / result documents.

The dictionaries are copied from the legacy bot so migrated patients see the same wording;
`otp_sent` uses `{n}` (the configured OTP length) instead of the hard-coded "4", and a few keys
were added for the new flow (contact button, expired code, generic error).
"""


from __future__ import annotations

import re

LANGS: tuple[str, ...] = ("uz", "uzc", "ru", "en")
DEFAULT_LANG = "uz"

PICK_LANG = "🌐 Tilni tanlang · Тилни танланг · Выберите язык · Choose language:"

TEXTS: dict[str, dict[str, str]] = {
    "uz": {
        "lang_name": "O'zbekcha",
        "lang_set": "✅ Til: O'zbekcha",
        "kb_updated": "Menyu yangilandi.",
        "welcome_back": "Xush kelibsiz, {name}! Siz allaqachon tizimga kirgansiz.",
        "ask_phone": "Assalomu alaykum! Iltimos, telefon raqamingizni yuboring (+998XXXXXXXXX formatida):",
        "btn_share_phone": "📱 Raqamni yuborish",
        "bad_phone": "Iltimos, telefon raqamni to'g'ri formatda yuboring (+998XXXXXXXXX yoki 998XXXXXXXXX yoki 90XXXXXXX):",
        "not_found": "Bu raqam tizimda topilmadi. Iltimos, tekshirib qaytadan yuboring:",
        "too_many": "Kod yaqinda yuborilgan. Biroz kutib, qaytadan urinib ko'ring.",
        "otp_sms": "Sizning tasdiqlash kodingiz: {otp}",
        "otp_sent": "Telefon raqamingizga {n} xonali kod yuborildi. Kodni kiriting:",
        "otp_expired": "❌ Kod muddati tugadi. Iltimos, /start buyrug'ini qaytadan yuboring.",
        "verified": "✅ Tasdiqlandi! {name}, botga xush kelibsiz.",
        "wrong_code": "❌ Noto'g'ri kod. {attempts} ta urinish qoldi. Qaytadan kiriting:",
        "attempts_over": "❌ Urinishlar soni tugadi. Iltimos, /start buyrug'ini qaytadan yuboring.",
        "cancelled": "Bekor qilindi.",
        "logged_out": "✅ {name}, siz tizimdan chiqdingiz.\nQayta kirish uchun /start buyrug'ini yuboring.",
        "not_logged_in": "Siz tizimga kirmagansiz. Kirish uchun /start buyrug'ini yuboring.",
        "need_login": "Avval tizimga kiring — /start buyrug'ini yuboring.",
        "default_user": "foydalanuvchi",
        "no_cheks": "Sizda hozircha cheklar yo'q.",
        "cheks_header": "🧾 Sizning cheklaringiz (oxirgi {n} ta):\n",
        "chek_no": "Chek №",
        "sum_label": "Summa",
        "status_label": "Holat",
        "paid": "✅ To'langan",
        "unpaid": "⏳ To'lanmagan",
        "som": "so'm",
        "no_payments": "Sizda hozircha to'lovlar yo'q.",
        "payments_header": "💰 To'lovlaringiz (oxirgi {n} ta):\n",
        "payments_total": "\nJami: {n} ta to'lov — {sum} so'm",
        "results_wait": "📄 Natijalaringiz tayyorlanmoqda, biroz kuting...",
        "result_default": "Tahlil natijasi",
        "no_results": "Sizda hozircha tayyor natija yo'q. Tahlil natijasi tasdiqlangandan so'ng bu yerda paydo bo'ladi.",
        "results_done": "✅ Jami {n} ta natija yuborildi.",
        "error": "Xatolik yuz berdi. Keyinroq qayta urinib ko'ring.",
        "btn_cheks": "🧾 Mening cheklarim",
        "btn_payments": "💰 To'lovlarim",
        "btn_results": "📄 Natijalarim",
        "btn_logout": "🚪 Chiqish",
        "btn_lang": "🌐 Tilni o'zgartirish",
    },
    "uzc": {
        "lang_name": "Ўзбекча (кирилл)",
        "lang_set": "✅ Тил: Ўзбекча (кирилл)",
        "kb_updated": "Меню янгиланди.",
        "welcome_back": "Хуш келибсиз, {name}! Сиз аллақачон тизимга киргансиз.",
        "ask_phone": "Ассалому алайкум! Илтимос, телефон рақамингизни юборинг (+998XXXXXXXXX форматида):",
        "btn_share_phone": "📱 Рақамни юбориш",
        "bad_phone": "Илтимос, телефон рақамни тўғри форматда юборинг (+998XXXXXXXXX ёки 998XXXXXXXXX ёки 90XXXXXXX):",
        "not_found": "Бу рақам тизимда топилмади. Илтимос, текшириб қайтадан юборинг:",
        "too_many": "Код яқинда юборилган. Бироз кутиб, қайтадан уриниб кўринг.",
        "otp_sms": "Сизнинг тасдиқлаш кодингиз: {otp}",
        "otp_sent": "Телефон рақамингизга {n} хонали код юборилди. Кодни киритинг:",
        "otp_expired": "❌ Код муддати тугади. Илтимос, /start буйруғини қайтадан юборинг.",
        "verified": "✅ Тасдиқланди! {name}, ботга хуш келибсиз.",
        "wrong_code": "❌ Нотўғри код. {attempts} та уриниш қолди. Қайтадан киритинг:",
        "attempts_over": "❌ Уринишлар сони тугади. Илтимос, /start буйруғини қайтадан юборинг.",
        "cancelled": "Бекор қилинди.",
        "logged_out": "✅ {name}, сиз тизимдан чиқдингиз.\nҚайта кириш учун /start буйруғини юборинг.",
        "not_logged_in": "Сиз тизимга кирмагансиз. Кириш учун /start буйруғини юборинг.",
        "need_login": "Аввал тизимга киринг — /start буйруғини юборинг.",
        "default_user": "фойдаланувчи",
        "no_cheks": "Сизда ҳозирча чеклар йўқ.",
        "cheks_header": "🧾 Сизнинг чекларингиз (охирги {n} та):\n",
        "chek_no": "Чек №",
        "sum_label": "Сумма",
        "status_label": "Ҳолат",
        "paid": "✅ Тўланган",
        "unpaid": "⏳ Тўланмаган",
        "som": "сўм",
        "no_payments": "Сизда ҳозирча тўловлар йўқ.",
        "payments_header": "💰 Тўловларингиз (охирги {n} та):\n",
        "payments_total": "\nЖами: {n} та тўлов — {sum} сўм",
        "results_wait": "📄 Натижаларингиз тайёрланмоқда, бироз кутинг...",
        "result_default": "Таҳлил натижаси",
        "no_results": "Сизда ҳозирча тайёр натижа йўқ. Таҳлил натижаси тасдиқлангандан сўнг бу ерда пайдо бўлади.",
        "results_done": "✅ Жами {n} та натижа юборилди.",
        "error": "Хатолик юз берди. Кейинроқ қайта уриниб кўринг.",
        "btn_cheks": "🧾 Менинг чекларим",
        "btn_payments": "💰 Тўловларим",
        "btn_results": "📄 Натижаларим",
        "btn_logout": "🚪 Чиқиш",
        "btn_lang": "🌐 Тилни ўзгартириш",
    },
    "ru": {
        "lang_name": "Русский",
        "lang_set": "✅ Язык: Русский",
        "kb_updated": "Меню обновлено.",
        "welcome_back": "С возвращением, {name}! Вы уже вошли в систему.",
        "ask_phone": "Здравствуйте! Пожалуйста, отправьте свой номер телефона (в формате +998XXXXXXXXX):",
        "btn_share_phone": "📱 Отправить номер",
        "bad_phone": "Пожалуйста, отправьте номер в правильном формате (+998XXXXXXXXX, 998XXXXXXXXX или 90XXXXXXX):",
        "not_found": "Этот номер не найден в системе. Проверьте и отправьте ещё раз:",
        "too_many": "Код уже отправлен недавно. Подождите немного и попробуйте снова.",
        "otp_sms": "Ваш код подтверждения: {otp}",
        "otp_sent": "На ваш номер отправлен {n}-значный код. Введите код:",
        "otp_expired": "❌ Срок действия кода истёк. Отправьте команду /start заново.",
        "verified": "✅ Подтверждено! {name}, добро пожаловать в бот.",
        "wrong_code": "❌ Неверный код. Осталось попыток: {attempts}. Введите ещё раз:",
        "attempts_over": "❌ Попытки закончились. Отправьте команду /start заново.",
        "cancelled": "Отменено.",
        "logged_out": "✅ {name}, вы вышли из системы.\nЧтобы войти снова, отправьте /start.",
        "not_logged_in": "Вы не вошли в систему. Отправьте /start, чтобы войти.",
        "need_login": "Сначала войдите в систему — отправьте /start.",
        "default_user": "пользователь",
        "no_cheks": "У вас пока нет чеков.",
        "cheks_header": "🧾 Ваши чеки (последние {n}):\n",
        "chek_no": "Чек №",
        "sum_label": "Сумма",
        "status_label": "Статус",
        "paid": "✅ Оплачен",
        "unpaid": "⏳ Не оплачен",
        "som": "сум",
        "no_payments": "У вас пока нет платежей.",
        "payments_header": "💰 Ваши платежи (последние {n}):\n",
        "payments_total": "\nИтого: {n} платежей — {sum} сум",
        "results_wait": "📄 Ваши результаты готовятся, подождите немного...",
        "result_default": "Результат анализа",
        "no_results": "У вас пока нет готовых результатов. Они появятся здесь после подтверждения анализа.",
        "results_done": "✅ Отправлено результатов: {n}.",
        "error": "Произошла ошибка. Попробуйте позже.",
        "btn_cheks": "🧾 Мои чеки",
        "btn_payments": "💰 Мои платежи",
        "btn_results": "📄 Мои результаты",
        "btn_logout": "🚪 Выйти",
        "btn_lang": "🌐 Изменить язык",
    },
    "en": {
        "lang_name": "English",
        "lang_set": "✅ Language: English",
        "kb_updated": "Menu updated.",
        "welcome_back": "Welcome back, {name}! You are already signed in.",
        "ask_phone": "Hello! Please send your phone number (format +998XXXXXXXXX):",
        "btn_share_phone": "📱 Share my number",
        "bad_phone": "Please send the phone number in a valid format (+998XXXXXXXXX, 998XXXXXXXXX or 90XXXXXXX):",
        "not_found": "This number was not found in the system. Check it and send again:",
        "too_many": "A code was sent recently. Please wait a moment and try again.",
        "otp_sms": "Your verification code: {otp}",
        "otp_sent": "A {n}-digit code has been sent to your phone. Enter the code:",
        "otp_expired": "❌ The code has expired. Please send /start again.",
        "verified": "✅ Verified! Welcome, {name}.",
        "wrong_code": "❌ Wrong code. {attempts} attempts left. Try again:",
        "attempts_over": "❌ No attempts left. Please send /start again.",
        "cancelled": "Cancelled.",
        "logged_out": "✅ {name}, you have signed out.\nSend /start to sign in again.",
        "not_logged_in": "You are not signed in. Send /start to sign in.",
        "need_login": "Please sign in first — send /start.",
        "default_user": "user",
        "no_cheks": "You have no checks yet.",
        "cheks_header": "🧾 Your checks (last {n}):\n",
        "chek_no": "Check #",
        "sum_label": "Amount",
        "status_label": "Status",
        "paid": "✅ Paid",
        "unpaid": "⏳ Unpaid",
        "som": "UZS",
        "no_payments": "You have no payments yet.",
        "payments_header": "💰 Your payments (last {n}):\n",
        "payments_total": "\nTotal: {n} payments — {sum} UZS",
        "results_wait": "📄 Preparing your results, please wait...",
        "result_default": "Test result",
        "no_results": "You have no ready results yet. They will appear here once your test is confirmed.",
        "results_done": "✅ {n} result(s) sent.",
        "error": "Something went wrong. Please try again later.",
        "btn_cheks": "🧾 My checks",
        "btn_payments": "💰 My payments",
        "btn_results": "📄 My results",
        "btn_logout": "🚪 Sign out",
        "btn_lang": "🌐 Change language",
    },
}

# Bot command menu per Telegram UI language (default = Uzbek, like the legacy bot).
COMMANDS: dict[str | None, list[tuple[str, str]]] = {
    None: [("start", "Tizimga kirish"), ("lang", "Tilni o'zgartirish"), ("logout", "Tizimdan chiqish"), ("cancel", "Amalni bekor qilish")],
    "ru": [("start", "Войти в систему"), ("lang", "Изменить язык"), ("logout", "Выйти"), ("cancel", "Отменить действие")],
    "en": [("start", "Sign in"), ("lang", "Change language"), ("logout", "Sign out"), ("cancel", "Cancel action")],
}


def norm_lang(lang: str | None) -> str:
    """Unknown / missing language → default (uz)."""
    return lang if lang in LANGS else DEFAULT_LANG


def t(lang: str | None, key: str, **kw: object) -> str:
    """Localised text; falls back to Uzbek, then to the key itself. `kw` are `str.format` fields."""
    s = TEXTS.get(norm_lang(lang), TEXTS[DEFAULT_LANG]).get(key) or TEXTS[DEFAULT_LANG].get(key, key)
    return s.format(**kw) if kw else s


def button_regex(key: str) -> str:
    """Regex matching a main-keyboard button in ANY language (old keyboards keep working after a language switch)."""
    return "^(" + "|".join(re.escape(TEXTS[lang][key]) for lang in LANGS) + ")$"
