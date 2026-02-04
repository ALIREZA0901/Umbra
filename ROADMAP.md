# Umbra v0.2 — Feature Map (Spec → Implementation)

این فایل «مپ اجرایی» است تا هر بخش برنامه جداگانه دیده شود و مسیر تکمیل پروژه واضح باشد.

## 1) App Launcher
- **Spec**: لیست مهم‌ترین اپ‌ها، اضافه‌کردن دستی، auto-detect، cache، اجرای گروهی، ری‌لانچ، پروفایل جدا.
- **وضعیت فعلی**: صفحه App Launcher با لیست مهم‌ترین اپ‌ها + افزودن دستی + تشخیص Running + افزودن برنامه‌های در حال اجرا + اجرا/توقف + فعال/غیرفعال + گروه‌بندی/جابجایی پیاده شد.
- **اقدام بعدی**:
  - Persistence (cache) برای اپ‌ها (مثلاً آخرین انتخاب‌ها/پروفایل‌ها)
  - پروفایل per-app

## 2) Network Binding (Per App Routing)
- **Spec**: انتخاب شبکه (Modem/Mobile/VDSL) برای هر اپ + نمایش Gateway/Subnet/Interface.
- **وضعیت فعلی**: صفحه App Routing ایجاد شده و route mapها ذخیره می‌شوند.
- **اقدام بعدی**:
  - تکمیل نمایش اطلاعات Interface (Gateway/Subnet)
  - پیاده‌سازی Policy Routing واقعی در ویندوز (WFP/Netsh/route rules)
  - پروفایل سریع برای OBS

## 3) DNS Settings (Per App)
- **Spec**: تنظیم DNS برای هر اپ + preset + apply/reset.
- **وضعیت فعلی**: لیست DNS و بهینه‌سازی safe ping موجود است.
- **اقدام بعدی**:
  - اعمال per-app DNS روی سیستم
  - presets قابل انتخاب
  - reset per app

## 4) UI / UX
- **Spec**: UI Dark شبیه Discord/TeamSpeak، Menu bar، Tray actions.
- **وضعیت فعلی**: تم تاریک + tray + پنل‌ها ساخته شده‌اند.
- **اقدام بعدی**:
  - Menu bar سریع
  - Quick actions در tray
  - ریزتنظیمات زیبایی

## 5) Auto Refresh / Monitoring
- **Spec**: refresh interval، toggle، pause on minimize.
- **وضعیت فعلی**: مانیتورینگ سبک در Dashboard و App Routing.
- **اقدام بعدی**:
  - تنظیم interval
  - pause/sleep هنگام minimize

## 6) VPN Manager (Inside Umbra)
- **Spec**: مدیریت config، پروفایل‌ها، core run/stop، پشتیبانی پروتکل‌ها.
- **وضعیت فعلی**: مدیریت config + subscription + import از clipboard موجود است.
- **اقدام بعدی**:
  - اجرای core واقعی
  - مدیریت پروفایل‌های VPN
  - SOCKS/HTTP/WireGuard/Hysteria2

## 7) VPN Port / Process Detection
- **Spec**: auto-detect port و route method داخلی.
- **وضعیت فعلی**: اسکلت Engine Manager آماده است.
- **اقدام بعدی**:
  - شناسایی پورت‌های listening
  - UI override دستی

## 8) Safety & “No Network-Impacting Tests”
- **Spec**: هیچ تست شبکه‌ای بدون تأیید.
- **وضعیت فعلی**: تمام تست‌ها (Ping/Advanced) با تأیید دستی هستند.
- **اقدام بعدی**:
  - پیام‌های تأیید واضح‌تر
  - گزینه خاموش‌کردن کامل اتومیشن‌ها

## 9) Streaming Helpers (OBS-focused)
- **Spec**: انتخاب شبکه برای OBS + پیشنهاد bitrate بدون تست سنگین.
- **وضعیت فعلی**: پیشنهاد bitrate بر اساس headroom موجود است.
- **اقدام بعدی**:
  - اتصال به پروفایل OBS (در صورت امکان)
  - نمایش بهتر توصیه‌ها

## 10) Future Expandability
- **Spec**: مودم سوم، VPN engines دیگر (OpenConnect/OpenVPN).
- **وضعیت فعلی**: صفحات دانلود/لینک آماده است.
- **اقدام بعدی**:
  - افزودن هسته‌های دیگر
  - ساخت لایه‌های سازگار

---

## Roadmap پیشنهادی نسخه‌ها
- **v0.2.1**: اجرای پایدار + UI جدید
- **v0.2.2**: Network binding + DNS
- **v0.2.3**: VPN manager + import clipboard
