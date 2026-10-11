"""Inline keyboards for the owner bot. Pure data — no auth logic here."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 حالة النظام", callback_data="menu:status")],
        [InlineKeyboardButton("🏢 الشركات والمكاتب", callback_data="menu:companies")],
        [InlineKeyboardButton("📋 الخطط المعلقة", callback_data="menu:plans")],
        [InlineKeyboardButton("🌴 طلبات الإجازة", callback_data="menu:leaves")],
        [InlineKeyboardButton("💰 المالية", callback_data="menu:finance")],
        [InlineKeyboardButton("📈 التقارير", callback_data="menu:reports")],
        [InlineKeyboardButton("💊 المنتجات", callback_data="menu:products")],
        [InlineKeyboardButton("📝 الاستبيانات", callback_data="menu:surveys")],
        [InlineKeyboardButton("👥 المستخدمون", callback_data="menu:users:0")],
        [InlineKeyboardButton("🔍 بحث عن مندوب", callback_data="menu:search_rep")],
        [InlineKeyboardButton("📝 سجل التدقيق", callback_data="menu:audit:0")],
        [InlineKeyboardButton("💾 النسخ الاحتياطية", callback_data="menu:backups")],
    ])


def confirm_keyboard(yes_data: str, back_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد", callback_data=yes_data)],
        [InlineKeyboardButton("❌ إلغاء", callback_data=back_data)],
    ])


def plan_review_keyboard(plan_type: str, plan_id: int,
                         office_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ اعتماد",
                              callback_data=f"act:plan_approve:{plan_type}:{plan_id}:{office_id}")],
        [InlineKeyboardButton("✏️ إرجاع للتعديل",
                              callback_data=f"act:plan_return:{plan_type}:{plan_id}:{office_id}")],
        [InlineKeyboardButton("◀️ الخطط المعلقة", callback_data="menu:plans")],
    ])


def rep_actions_keyboard(office_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ إنهاء الارتباط", callback_data=f"act:rep_term:{office_id}:{user_id}")],
        [InlineKeyboardButton("🔀 نقل لمكتب آخر", callback_data=f"act:rep_transfer:{office_id}:{user_id}")],
        [InlineKeyboardButton("◀️ المندوبون", callback_data=f"menu:reps:{office_id}:0")],
    ])


def offices_pick(prefix: str, offices, exclude_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🏬 {o.name_ar}",
                                  callback_data=f"{prefix}:{o.id}")]
            for o in offices if o.id != exclude_id]
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def roles_pick(prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(name, callback_data=f"{prefix}:{role}")]
            for role, name in (("manager", "👔 مدير"), ("supervisor", "🧭 مشرف"),
                               ("rep", "🩺 مندوب"))]
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ القائمة الرئيسية", callback_data="menu:main")]
    ])


def leave_review_keyboard(office_id: int, request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ اعتماد الإجازة",
                              callback_data=f"act:leave_approve:{office_id}:{request_id}")],
        [InlineKeyboardButton("❌ رفض الإجازة",
                              callback_data=f"act:leave_reject:{office_id}:{request_id}")],
        [InlineKeyboardButton("◀️ طلبات الإجازة", callback_data="menu:leaves")],
    ])


def deduct_review_keyboard(office_id: int, txn_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ اعتماد الخصم",
                              callback_data=f"act:deduct_approve:{office_id}:{txn_uuid}")],
        [InlineKeyboardButton("❌ رفض الخصم",
                              callback_data=f"act:deduct_reject:{office_id}:{txn_uuid}")],
        [InlineKeyboardButton("◀️ الخصومات المقترحة", callback_data="menu:deducts")],
    ])


def product_detail_keyboard(office_id: int, product_id: int,
                            is_active: bool) -> InlineKeyboardMarkup:
    toggle = "⏸️ تعطيل" if is_active else "▶️ تفعيل"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle,
                              callback_data=f"act:product_toggle:{office_id}:{product_id}")],
        [InlineKeyboardButton("◀️ المنتجات",
                              callback_data=f"menu:products_off:{office_id}")],
    ])


def survey_detail_keyboard(office_id: int, survey_id: int,
                           status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "draft":
        rows.append([InlineKeyboardButton(
            "▶️ تفعيل", callback_data=f"act:survey_activate:{office_id}:{survey_id}")])
    elif status == "active":
        rows.append([InlineKeyboardButton(
            "⏹️ إغلاق", callback_data=f"act:survey_close:{office_id}:{survey_id}")])
    rows.append([InlineKeyboardButton(
        "📊 عرض النتائج",
        callback_data=f"menu:survey_results:{office_id}:{survey_id}")])
    rows.append([InlineKeyboardButton(
        "◀️ الاستبيانات", callback_data=f"menu:surveys_off:{office_id}")])
    return InlineKeyboardMarkup(rows)


def companies_list(companies) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🏢 {c.name_ar}",
                                 callback_data=f"menu:company:{c.id}")]
            for c in companies]
    rows.append([InlineKeyboardButton("➕ شركة جديدة", callback_data="act:company_add")])
    rows.append([InlineKeyboardButton("◀️ القائمة الرئيسية", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def company_detail_keyboard(company_id: int, offices) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🏬 {o.name_ar} — {o.city}",
                                  callback_data=f"menu:office:{o.id}")]
            for o in offices]
    rows.append([InlineKeyboardButton("➕ مكتب جديد",
                                      callback_data=f"act:office_add:{company_id}")])
    rows.append([InlineKeyboardButton("◀️ الشركات",
                                      callback_data="menu:companies")])
    return InlineKeyboardMarkup(rows)


def users_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ مستخدم جديد", callback_data="act:user_add")],
        [InlineKeyboardButton("◀️ القائمة الرئيسية", callback_data="menu:main")],
    ])


def offices_list(company_id: int, offices) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🏬 {o.name_ar} — {o.city}",
                                 callback_data=f"menu:office:{o.id}")]
            for o in offices]
    rows.append([InlineKeyboardButton("◀️ الشركات", callback_data="menu:companies")])
    return InlineKeyboardMarkup(rows)


def office_detail(office_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🩺 المندوبون", callback_data=f"menu:reps:{office_id}:0")],
        [InlineKeyboardButton("◀️ المكاتب", callback_data="menu:companies")],
        [InlineKeyboardButton("◀️ القائمة الرئيسية", callback_data="menu:main")],
    ])


def paginated(prefix: str, page: int, has_next: bool,
              extra_rows=None) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ السابق",
                                       callback_data=f"{prefix}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("التالي ▶️",
                                       callback_data=f"{prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    if extra_rows:
        rows.extend(extra_rows)
    rows.append([InlineKeyboardButton("◀️ القائمة الرئيسية", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)
