# DATABASE.md — تصميم قواعد البيانات

## 1. القاعدة المركزية: `data/identity.db`

الحد الأدنى اللازم للهوية والتوجيه فقط.

### companies
| عمود | نوع | ملاحظة |
|---|---|---|
| id | INTEGER PK | |
| name_ar / name_en | TEXT | |
| status | TEXT | active / suspended |
| timezone | TEXT | افتراضي `Asia/Baghdad` |
| created_at | TIMESTAMP UTC | |

### offices
| عمود | نوع | ملاحظة |
|---|---|---|
| id | INTEGER PK | |
| company_id | INTEGER FK → companies | |
| name_ar / name_en | TEXT | |
| city / area | TEXT | بغداد/المحافظات |
| timezone | TEXT | قابل للتغيير لكل مكتب |
| work_start / work_end | TEXT | `08:00`/`16:00` مثلًا |
| work_days | TEXT | JSON: `[0,1,2,3,4]` |
| db_path | TEXT | `data/office_{id}.db` — يحدده الخادم فقط |
| status | TEXT | active / suspended |
| created_at | TIMESTAMP UTC | |

### users
| عمود | نوع | ملاحظة |
|---|---|---|
| id | INTEGER PK | |
| full_name | TEXT | عربي/إنجليزي |
| phone | TEXT UNIQUE | وسيلة الربط مع WhatsApp لاحقًا |
| telegram_id | INTEGER UNIQUE NULL | للـ Owner والمديرين |
| password_hash | TEXT NULL | للواجهات التي تحتاجها (argon2 لاحقًا) |
| status | TEXT | active / disabled |

### memberships (العضوية = هوية المستخدم داخل مكتب)
| عمود | نوع | ملاحظة |
|---|---|---|
| id | INTEGER PK | |
| user_id | INTEGER FK → users | |
| company_id | INTEGER FK → companies | |
| office_id | INTEGER FK → offices NULL | NULL = عضوية على مستوى الشركة |
| role | TEXT | owner / manager / supervisor / rep |
| status | TEXT | active / ended |
| started_at / ended_at | TIMESTAMP UTC | تاريخ الارتباط — **لا يُحذف أبدًا** |

> **قاعدة:** إحلال `ended_at` بدل الحذف يحفظ السجل التاريخي (الاستقالة/النقل).

### central_audit_events
`(id, occurred_at UTC, actor_user_id, company_id, office_id, event_type, entity,
entity_id, result, reason, request_id, meta_json)` — للعمليات الحساسة العابرة.

## 2. قاعدة المكتب: `data/office_{id}.db`

### representatives
`(id, user_id, employee_code UNIQUE, supervisor_user_id NULL, hire_date,
job_status, notes)` — `user_id` يربط بالهوية المركزية؛ البيانات الوظيفية هنا.

### doctors / pharmacies
`(id, name, specialty/type, address, phone, area, status, created_by, created_at)`
مع قيد منع التكرار على `(name, phone)` لمنع السجلات المكررة.

### weekly_plans / daily_plans
`(id, representative_id, period/date, status, payload_json, version,
submitted_at, reviewed_by, reviewed_at, review_notes, created_at, updated_at)`
- الحالات: `draft → submitted → under_review → approved | changes_requested → superseded`
- كل تعديل = صف `weekly_plan_revisions` جديد، **لا** مسح للقديم.

### planned_visits / actual_visits
- `planned_visits`: `(id, plan_id, doctor_id/pharmacy_id, expected_order, expected_time)`
- `actual_visits`: `(id, representative_id, doctor_id/pharmacy_id, visited_at UTC,
  visit_type, outcome, notes, source, status, correction_of NULL)`
- **الفصل صارم:** المخطط لا يُحتسب فعليًا أبدًا. التصحيح = سجل جديد يشير للأصل.

### attendance_records
`(id, representative_id, date, check_in_at UTC, check_out_at UTC, source,
status, late_minutes)` — وقت الخادم فقط، لا وقت المستخدم.

### leave_requests
`(id, representative_id, start_date, end_date, leave_type, reason, status,
decided_by, decided_at, decision_reason)` — الحالات: pending/approved/rejected/cancelled.

### payroll_periods / salary_records / financial_transactions
- `payroll_periods`: `(id, office_id, year, month, status)` — open/locked.
- `salary_records`: `(id, representative_id, period_id, base_salary)` — الأساس.
- `financial_transactions`: `(id, uuid UNIQUE, representative_id, period_id, kind,
  amount, reason, status, actor_user_id, source, created_at)`
  - `kind`: deduction / bonus / increase
  - `status`: proposed / approved / rejected / applied
  - `uuid` = مفتاح idempotency: تكرار نفس UUID **مستحيل** أن يكرر الحركة.
  - الراتب الصافي = الأساسي − المعتمد من الخصومات + المعتمد من الزيادات.

### products / product_units
`(id, trade_name, scientific_name, manufacturer, origin_country, dosage_form,
strength, pack_size, units_per_pack, barcode NULL, notes)` — فصل العبوة عن الوحدة.

### surveys / survey_responses / survey_attachments
نماذج قابلة للتهيئة (`surveys.definition_json`) + ردود + مرفقات (نوع/حجم متحقق منه).

### notification_jobs / notification_delivery_logs
مهام الإشعارات وحالة كل إرسال (pending/sent/failed + السبب) — لا ادعاء كاذب بالإرسال.

### office_audit_events
نفس هيكل المركزي لكن على مستوى المكتب لكل العمليات التشغيلية.

### backup_records (مخطط v2)
`(id, taken_at, file_name, sha256, size_bytes, encrypted, verified, restored_test_at)`
— تُكتب عند كل نسخة احتياطية مع تحقق فوري (فك تشفير + integrity_check).

## 3. الترحيل (Alembic)

### 3.1 الهوية المركزية — Alembic كامل
مجلد `alembic/` يدير `identity.db` عبر `alembic upgrade head`.
الترقية الأولى `0001_create_identity_schema` تطابق `app/identity/models.py`.

### 3.2 قواعد المكاتب — Alembic مؤجّل لسبب تقني موثّق
**السبب:** ملفات قواعد المكاتب تُنشأ ديناميكيًا وقت التشغيل (مكتب جديد =
ملف جديد). Alembic مصمَّم لقائمة ثابتة من قواعد البيانات، لا لملفات
تظهر وتختفي. تشغيله لكل ملف على حدة يضيف تعقيدًا دون فائدة.

**البديل المنفَّذ:** كل ملف مكتب يحمل جدول `schema_version`، ودالة
`migrate_office_db()` في `app/databases/manager.py` تطبّق الترقيات
مرتّبةً داخل آلية ذرّية على مستوى الملف (نسخة `.bak` + استعادة عند الفشل —
لأن pysqlite يُصدر COMMIT ضمنيًا قبل DDL، مُثبت تجريبيًا في R6).
دوال الترقية يجب أن تكون idempotent.

**سجل النسخ:**
- v1: المندوبون + التدقيق
- v2: `backup_records`
- v3: الأطباء/الصيدليات، الخطط (أسبوعية/يومية + مراجعات + زيارات مخططة)،
  الزيارات الفعلية، الحضور، `notification_jobs`
- v4: المنتجات والوحدات، أنواع الإجازات وطلباتها، الاجتماعات والحضور،
  العطل، الاستبيانات (أسئلة/ردود/إجابات)، المرفقات (بيانات وصفية)
- v5: فترات الرواتب، سجلات الرواتب، دفتر الحركات المالية (append-only + uuid)
- v6: إثراء ملف الطبيب (منطقة فرعية/صيدلية/تصنيف/استقبال/حالة/كود خارجي/version)،
  منتجات TGT، سجل تغييرات الأطباء، الاستبيانات الميدانية، سجل الزيادات الأساسي،
  سجل الاستيرادات

**شرط إعادة التقييم:** إذا تجاوزت ترقيات مخطط المكاتب 5 نسخ أو أصبحت
معقّدة، يُعاد النظر في Alembic لكل ملف عبر `alembic -x office=<id>`.

## 4. التزامن والنسخ الاحتياطي (SQLite)

- تفعيل `journal_mode=WAL` + `busy_timeout=5000ms` عند كل اتصال.
- النسخ: `VACUUM INTO` لملف مؤقت → تشفير Fernet بمفتاح المكتب → نقل خارج الجهاز.
- الاستعادة: فك التشفير → فحص السلامة (`PRAGMA integrity_check`) → استبدال
  بتأكيد صريح + تسجيل في التدقيق. لا كتابة فوق الحالي دون تأكيد.
