# hydrawise-report — دليل الاستخدام بالعربية

أداة تتصل بوحدة تحكم الري [Hydrawise](https://www.hydrawise.com/) من Hunter،
وتسجّل ساعات تشغيل كل محبس، ثم تحوّلها إلى **استهلاك مياه بالمتر المكعب،
واستهلاك كهرباء بالكيلوواط/ساعة، وتكلفة شهرية لكل شخص**، وترسل لكل شخص تقريره
على بريده الإلكتروني.

الأداة مكتوبة بلغة Python بدون أي مكتبات خارجية.

---

## ١. مفتاح الـ API — وليس كلمة المرور

كل الاتصال يتم عبر **مفتاح API** تحصل عليه من موقع Hydrawise:

> app.hydrawise.com ← **My Account** ← **Account Details** ← API key

```bash
export HYDRAWISE_API_KEY='مفتاحك-هنا'
```

هذا المفتاح يعادل صلاحية تشغيل الري، فاحفظه في متغيرات البيئة ولا تضعه داخل
الكود أو في Git. هذه الأداة لا تطلب كلمة مرور حسابك ولا تخزّنها ولا ترسلها.
إن سبق أن شاركت كلمة المرور في أي مكان، غيّرها فوراً واستخدم مفتاح API بدلاً
منها.

## ٢. التثبيت

```bash
pip install -e .
# أو التشغيل مباشرة من المجلد:
python -m hydrawise --help
```

يتطلب Python 3.9 أو أحدث.

## ٣. الاستعراض والتحكم

```bash
hydrawise controllers                 # وحدات التحكم في الحساب
hydrawise status                      # حالة المحابس وما يعمل الآن
hydrawise run "Front lawn" --minutes 10   # تشغيل محبس ١٠ دقائق
hydrawise stop --all                  # إيقاف الكل
hydrawise suspend 2 --days 3          # تعليق المحبس رقم ٢ لثلاثة أيام
hydrawise resume 2                    # إلغاء التعليق
```

يمكن تحديد المحبس برقمه على الوحدة، أو بـ `relay_id`، أو باسمه.

## ٤. ملف الإعدادات

```bash
hydrawise init-config     # ينشئ hydrawise.config.json
```

هنا تضع المعلومات التي لا تعرفها وحدة التحكم: معدل تدفق كل محبس، قدرة المضخة،
التعرفة، ومالك كل محبس:

```jsonc
{
  "timezone": "Asia/Riyadh",
  "currency": "SAR",
  "water":       { "tariff_per_m3": 3.0 },
  "electricity": { "tariff_per_kwh": 0.18, "default_pump_kw": 2.2 },

  "people": [
    { "id": "ahmed", "name": "أحمد", "email": "ahmed@example.com", "language": "ar" }
  ],
  "zones": [
    { "zone": 1, "name": "المسطح الأمامي", "flow_rate_lpm": 40.0, "pump_kw": 2.2, "owner": "ahmed" }
  ],

  "email": {
    "smtp_host": "smtp.gmail.com", "smtp_port": 587,
    "username": "you@example.com", "from_address": "you@example.com",
    "password_env": "HYDRAWISE_SMTP_PASSWORD"
  }
}
```

`flow_rate_lpm` هو تدفق المحبس **باللتر في الدقيقة**. خذه من جدول البخاخات/
النقاطات، أو قِسه مرة واحدة: شغّل المحبس دقيقة واحدة واملأ دلواً مدرّجاً.
بدون هذه القيمة يظهر المحبس في التقرير بساعات التشغيل فقط، ويظهر عمود المياه
بعلامة `—` بدلاً من رقم غير صحيح.

كلمات السر تُقرأ من متغيرات البيئة عبر `api_key_env` و `password_env`، ولا
تُكتب داخل ملف الإعدادات.

## ٥. تسجيل ساعات التشغيل

واجهة Hydrawise العامة تعطي **ما يعمل الآن** فقط، ولا توجد فيها واجهة لسجل
الري التاريخي. لذلك نشغّل مسجّلاً يستعلم عن الحالة ويبني السجل محلياً:

```bash
hydrawise poll --interval 60      # يعمل باستمرار — Ctrl-C للإيقاف
hydrawise poll --once             # استعلام واحد (مناسب لـ cron)
hydrawise runs --month 2026-08    # عرض ما تم تسجيله
```

يُفضّل تشغيله كخدمة دائمة (systemd) حتى لا تفوت أي دورة ري — انظر المثال في
[`README.md`](../README.md).

## ٦. التقارير الشهرية

```bash
hydrawise report                                    # الشهر الماضي، لكل شخص
hydrawise report --month 2026-08 --format csv --output august.csv
hydrawise report --month 2026-08 --format html --person ahmed

hydrawise send-reports --month 2026-08 --dry-run    # تجهيز بدون إرسال
export HYDRAWISE_SMTP_PASSWORD='كلمة-مرور-التطبيق'
hydrawise send-reports --month 2026-08 --skip-empty
```

الشخص الذي `language` عنده `"ar"` يصله التقرير بالعربية ومنسّقاً من اليمين
إلى اليسار.

للإرسال التلقائي أول كل شهر عبر cron:

```cron
0 6 1 * * cd /opt/hydrawise && HYDRAWISE_API_KEY=... HYDRAWISE_SMTP_PASSWORD=... \
          .venv/bin/hydrawise send-reports --skip-empty >> /var/log/hydrawise-mail.log 2>&1
```

## ٧. طريقة الحساب

```
المياه (م³)      = معدل التدفق (لتر/دقيقة) × دقائق التشغيل ÷ 1000
الكهرباء (ك.و.س) = قدرة المضخة (ك.و) × ساعات التشغيل
التكلفة          = م³ × تعرفة المياه + ك.و.س × تعرفة الكهرباء
```

ملاحظات مهمة على الدقة:

* الأرقام **تقديرية مبنية على ساعات التشغيل**، ودقتها من دقة `flow_rate_lpm`.
* المحبس بلا معدل تدفق لا تُحتسب مياهه، وينبّه التقرير على ذلك.
* الكهرباء تُنسب لصاحب المحبس الذي كانت المضخة تعمل من أجله. إن كان الري على
  ضغط الشبكة بلا مضخة، اجعل `"pump_kw": 0`.
* دورة الري تُحتسب على الشهر الذي **بدأت** فيه، ولا تُقسَّم بين شهرين.
* إن كان لديك **عدّاد تدفق (flow meter)** موصول بالوحدة، فقراءاته في تطبيق
  Hydrawise أدق من هذا التقدير — قارن بينهما مرة واحدة بعد أول شهر.

## ٨. الاختبارات

```bash
python -m unittest discover -s tests -t .
```

١١٨ اختباراً، بلا اتصال بالإنترنت وبلا مكتبات خارجية.
