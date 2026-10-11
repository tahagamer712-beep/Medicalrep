# DEPLOYMENT.md — التشغيل والتهيئة والنسخ الاحتياطي

> لا توجد أسرار حقيقية في هذا الملف. كل القيم الحساسة تُملأ في `.env`
> (غير مرفوع) فقط.

## 1. المتطلبات

- Python 3.12+
- Linux (أي توزيعة) — يعمل أيضًا على Termux/Android

## 2. التثبيت

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
chmod 600 .env
```

## 3. متغيرات البيئة (`.env`)

| المتغير | الوصف |
|---|---|
| `TELEGRAM_OWNER_IDS` | معرّفات Telegram الرقمية للـ Owner، مفصولة بفواصل (تُعرف من `@userinfobot`) |
| `TELEGRAM_OWNER_BOT_TOKEN` | توكن بوت الـ Owner من `@BotFather` |
| `DATA_DIR` | مسار مجلد البيانات (الافتراضي `./data`) |
| `APP_ENV` | `dev` / `prod` |

> مفاتيح تشفير النسخ **لا** تُضبط هنا — تُولَّد تلقائيًا لكل مكتب في
> `data/office_keys/` (انظر §8).

## 4. التهيئة من الصفر

```bash
source .venv/bin/activate
export MEDREP_DATA_DIR=./data        # أو DATA_DIR في .env

# 1) ترقية قاعدة الهوية المركزية
alembic upgrade head

# 2) إنشاء مالك النظام (owner) — مرة واحدة، إدراج مباشر (لا يوجد فاعل بعد):
python - <<'EOF'
from pathlib import Path
from app.databases import manager as dbm
from app.identity import models as im
d = Path("./data")
with dbm.identity_session(d) as s:
    u = im.User(full_name="المالك", phone="07000000000",
                telegram_id=<TELEGRAM_ID>)   # ضع رقمك من @userinfobot
    s.add(u); s.flush()
    s.add(im.Membership(user_id=u.id, company_id=None, office_id=None,
                        role="owner"))
print("owner user id:", u.id)
EOF
```

> النمط أعلاه هو نفس المستخدم في `scripts/init_demo.py` (سطور 57-66).

## 5. إنشاء شركة ومكتب ومستخدمين

الطريقة المدعومة إنتاجيًا: **بوت الـ Owner** (بعد تشغيله، §6):

1. `/menu` ← 🏢 الشركات ← ➕ شركة جديدة (الاسم العربي ثم الإنجليزي ← تأكيد)
2. تفاصيل الشركة ← ➕ مكتب جديد (الاسم العربي ثم الإنجليزي ← تأكيد)
   — تُنشأ قاعدة `office_{id}.db` تلقائيًا بأحدث مخطط (v6).
3. 👥 المستخدمون ← ➕ مستخدم جديد (الاسم ← الهاتف ← المكتب ← الدور ← تأكيد).

كل خطوة تفحص الصلاحية خدميًا، والإجراءات المدمرة تطلب تأكيدًا صريحًا.

## 6. تشغيل بوت الـ Owner والمهام المجدولة

```bash
source .venv/bin/activate
python scripts/run_owner_bot.py
```

يعمل في المقدمة (foreground) ويشغّل معًا:
1. **بوت الـ Owner** (polling).
2. **المهام المجدولة**: كل دقيقة تُنفَّذ التذكيرات المستحقة، وكل 30 دقيقة
   تُنشأ مهام المستقبل. التشغيل مكرر-آمن: `start_runner` يعيد المجدول
   الجاري عند الاستدعاء المكرر بدل تشغيل نسخة ثانية، والإيقاف
   (`stop_runner`) وإعادة التشغيل لا يكرران أي تذكير — الحالة كلها في
   قاعدة البيانات (`job_key` فريد + آلة حالات).

للإبقاء عليه دائمًا استخدم `systemd` أو `nohup` حسب بيئتك.

> **تحذير صريح:** المرسِل الحقيقي (Telegram) **غير مربوط بعد** — المهام
> تُنشأ وتُجدول وتُسجَّل عبر `LoggingNotifier` في الإنتاج، وتُختبر عبر
> `FakeNotifier` في الاختبارات. الربط بالإرسال الحقيقي خطوة لاحقة قبل الإنتاج.

## 7. النسخ الاحتياطي المشفّر والاستعادة

```bash
source .venv/bin/activate
export MEDREP_DATA_DIR=./data

# نسخ احتياطي لمكتب (المفتاح يُولَّد تلقائيًا لكل مكتب عند أول نسخة):
python scripts/backup.py --office-id 1
# → data/backups/office_1_<timestamp>.enc (مشفرة بـ Fernet)

# التحقق ثم الاستعادة (تتطلب --confirm صريحًا):
python scripts/restore.py --office-id 1 \
    --file data/backups/office_1_<timestamp>.enc --confirm
```

### النسخ الكامل (الهوية + المكاتب)

قاعدة الهوية المركزية (`identity.db`) تحمل المستخدمين والشركات والعضويات —
نسخ المكاتب وحدها **لا تكفي**. النسخة الكاملة = الهوية + كل المكاتب،
تُؤخذ معًا:

```bash
python scripts/backup.py --all        # الهوية أولًا ثم كل المكاتب
python scripts/backup.py --identity   # الهوية فقط
```

الاستعادة — **بالترتيب**: الهوية أولًا ثم المكاتب، ومن نسخ مأخوذة معًا:

```bash
python scripts/restore.py --identity \
    --file data/backups/identity_<timestamp>.enc --confirm
python scripts/restore.py --office-id 1 \
    --file data/backups/office_1_<timestamp>.enc --confirm
```

ملاحظات:
- نسخ الهوية مشفّرة بنفس الطريقة (مفتاح `data/office_keys/identity.key`).
- عند الاستعادة على جهاز مختلف تُعاد كتابة مسارات قواعد المكاتب تلقائيًا
  لتشير إلى مجلد البيانات الحالي — لا حاجة لتعديل يدوي.
- لا تخلط نسخًا من أوقات مختلفة: هوية من يوم ومكتب من يوم آخر قد
  تتعارضان (مستخدم محذوف في واحدة وموجود في الأخرى).

قواعد صارمة:
- مفاتيح التشفير **لكل مكتب** في `data/office_keys/office_{id}.key` بصلاحيات `0600`
  (تُولَّد تلقائيًا، لا تكتبها يدويًا).
- **انسخ مجلد `data/office_keys/` خارج الجهاز** — فقدان المفتاح = فقدان النسخ (K2).
- المفاتيح لا توضع بجانب النسخ أبدًا.
- الاستعادة لا تكتب فوق البيانات الحية بصمت (`--confirm` إجباري).

## 8. ترقية المخطط

قواعد المكاتب تُرقَّى تلقائيًا عند أول وصول (`migrate_office_db`)،
والترقيات ذرّية ومُختبرة (`tests/test_office_upgrade.py` — تشمل v3→v6).

## 9. ما لم يُختبر حقيقيًا بعد (لا تعتبره جاهزًا)

- [ ] اتصال Telegram حقيقي (البوت مختبر بـ mocks فقط).
- [ ] إرسال التذكيرات عبر Telegram فعليًا (حاليًا `LoggingNotifier` يسجّل فقط).
- [ ] التشغيل المستمر (systemd/nohup) على جهاز حقيقي.
- [ ] استعادة نسخة على جهاز مختلف.
- [ ] **نسخ `data/office_keys/` خارج الجهاز** — بدونه لا قيمة للنسخ (K2).
