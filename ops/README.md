# التشغيل المستمر (ops)

## التثبيت كخدمة systemd (موصى به إنتاجيًا)

```bash
sudo cp ops/medrep-bot.service /etc/systemd/system/
sudo mkdir -p /var/log/medrep && sudo chown medrep:medrep /var/log/medrep
sudo cp ops/medrep-logrotate /etc/logrotate.d/medrep
sudo systemctl daemon-reload
sudo systemctl enable --now medrep-bot
```

## الأوامر اليومية

```bash
sudo systemctl status medrep-bot      # الحالة
sudo systemctl restart medrep-bot     # إعادة تشغيل (آمنة: لا تكرار للتذكيرات)
sudo systemctl stop medrep-bot       # إيقاف
tail -f /var/log/medrep/bot.log       # السجلات (التذكيرات تُسجَّل هنا بوضع log)
```

## ملاحظات

- إعادة التشغيل آمنة: حالة المهام في قاعدة البيانات، والمجدول مكرر-آمن.
- `Restart=always` يعيد التشغيل تلقائيًا عند الأعطال.
- تدوير السجلات يوميًا مع الاحتفاظ بـ 30 يومًا.
- وضع الإرسال: `REMINDER_DELIVERY=log` (افتراضي، بلا إرسال حقيقي) أو
  `telegram` (إرسال حقيقي — يتطلب موافقة المالك وإعداد التوكن).
- بديل مؤقت (ليس إنتاجيًا): `nohup python scripts/run_owner_bot.py &`
