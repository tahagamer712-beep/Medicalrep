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
   — تُنشأ قاعدة `office_{id}.db` تلقائيًا بأحدث مخطط (v8).
3. 👥 المستخدمون ← ➕ مستخدم جديد (الاسم ← الهاتف ← المكتب ← الدور ← تأكيد).

كل خطوة تفحص الصلاحية خدميًا، والإجراءات المدمرة تطلب تأكيدًا صريحًا.

## 6. تشغيل بوت الـ Owner والمهام المجدولة

الطريقة المختصرة (Termux وLinux):

```bash
bash scripts/start.sh    # تشغيل في الخلفية (بوت + مجدول)
bash scripts/status.sh   # الحالة + آخر السجلات
bash scripts/restart.sh  # إعادة تشغيل (آمنة: لا تكرار للتذكيرات)
bash scripts/stop.sh     # إيقاف
```

`start.sh` يتحقق من `.env` (ينسخ `.env.example` تلقائيًا أول مرة بصلاحيات
600) ومن وجود التوكن ومعرّف المالك، ويحفظ الـ PID في `logs/bot.pid`
والسجلات في `logs/bot.log`. على Termux يفعّل `termux-wake-lock` إن وُجد.

التشغيل اليدوي في المقدمة (للتصحيح فقط):

```bash
source .venv/bin/activate
python scripts/run_owner_bot.py
```

يعمل معًا:
1. **بوت الـ Owner** (polling).
2. **المهام المجدولة**: كل دقيقة تُنفَّذ التذكيرات المستحقة، وكل 30 دقيقة
   تُنشأ مهام المستقبل. التشغيل مكرر-آمن: `start_runner` يعيد المجدول
   الجاري عند الاستدعاء المكرر بدل تشغيل نسخة ثانية — الحالة كلها في
   قاعدة البيانات (`job_key` فريد + آلة حالات).

وضع الإرسال (`REMINDER_DELIVERY` في `.env` أو البيئة):
- `log` (الافتراضي، الآمن): التذكيرات تُسجَّل في السجل فقط — **لا إرسال حقيقي**.
- `telegram`: إرسال حقيقي عبر `TelegramNotifier` — لا تفعّله إلا بعد
  اختبار البوت وموافقة المالك الصريحة.

للتشغيل الدائم على خادم Linux: `ops/medrep-bot.service` (systemd) —
التفاصيل في `ops/README.md`.

### خادم الـ API (لـ WhatsApp webhook)

```bash
bash scripts/run_api.sh        # المنفذ 8000 افتراضيًا
bash scripts/run_api.sh 8080   # منفذ مخصص
```

يخدم `/health` و`/whatsapp/webhook`. الـ webhook عند Meta يحتاج عنوانًا
عامًا بـ HTTPS — محليًا يكفي للفحص عبر `/health` فقط.

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
- **مجموعة النسخ (manifest)**: `backup.py --all` ينشئ نسخة هوية + كل
  المكاتب + ملف manifest يربطها مع SHA-256؛
  `restore.py --manifest <file> --confirm` يتحقق أولًا ثم يستعيد الهوية
  فالمكاتب، ويبلّغ بوضوح عن أي فشل جزئي.
- **لقطة أمان**: قبل كل استعادة تُنسخ القاعدة الحية إلى
  `data/backups/safety/<timestamp>/` — إن كانت الاستعادة خطأً يمكنك
  الرجوع يدويًا بنسخ الملف القديم مكانه.

قواعد صارمة:
- مفاتيح التشفير **لكل مكتب** في `data/office_keys/office_{id}.key` بصلاحيات `0600`
  (تُولَّد تلقائيًا، لا تكتبها يدويًا).
- **انسخ مجلد `data/office_keys/` خارج الجهاز** — فقدان المفتاح = فقدان النسخ (K2).
- المفاتيح لا توضع بجانب النسخ أبدًا.
- الاستعادة لا تكتب فوق البيانات الحية بصمت (`--confirm` إجباري).

## 8. ترقية المخطط

قواعد المكاتب تُرقَّى تلقائيًا عند أول وصول (`migrate_office_db`)،
والترقيات ذرّية ومُختبرة (`tests/test_office_upgrade.py` — تشمل v6→v8
مع حفظ البيانات).

## 9. ما لم يُختبر حقيقيًا بعد (لا تعتبره جاهزًا)

- [ ] اتصال Telegram حقيقي (البوت مختبر بـ mocks فقط) — **هذا هو المطلوب
  منك: ضع التوكن ومعرّف المالك في `.env` ثم `bash scripts/start.sh`**.
- [ ] إرسال التذكيرات عبر Telegram فعليًا — الكود جاهز
  (`TelegramNotifier`) لكن الوضع الافتراضي `log`؛ التفعيل بموافقتك فقط.
- [ ] WhatsApp حقيقي — الكود الرسمي جاهز (webhook/توقيع/قوالب/نافذة 24h)
  لكنه معطّل حتى تجهّز حساب Meta وتوافق.
- [ ] التشغيل المستمر على جهاز حقيقي (السكربتات وsystemd جاهزة).
- [ ] **نسخ `data/office_keys/` خارج الجهاز** — بدونه لا قيمة للنسخ (K2).
