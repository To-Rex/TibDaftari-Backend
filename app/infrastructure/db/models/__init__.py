"""All ORM models — import this module so Alembic and the app see every table."""

from app.infrastructure.db.models.catalog import (
    AttributeSchema,
    Category,
    ResultTemplate,
    ServiceType,
    ServiceUnitSetting,
    StoredFile,
    TemplateAsset,
    TemplateUnit,
)
from app.infrastructure.db.models.geo import Country, District, Region
from app.infrastructure.db.models.messaging import AuditLog, MessagingAccount, Notification, OutboxMessage, SmsTemplate
from app.infrastructure.db.models.order import (
    ITEM_STATUSES,
    ORDER_STATUSES,
    PAYMENT_STATUSES,
    Order,
    OrderItem,
    Payment,
    ResultDocument,
    empty_progress,
)
from app.infrastructure.db.models.org import (
    CHILD_LEVEL,
    FACILITY_KINDS,
    PARENT_LEVEL,
    UNIT_LEVELS,
    OrderCounter,
    OrgUnit,
    OrgUnitClosure,
)
from app.infrastructure.db.models.patient import Patient, TelegramChatPref, TelegramLink
from app.infrastructure.db.models.staff import Employee, EmployeeUnit, OtpChallenge, Role, Session

__all__ = [
    "CHILD_LEVEL", "FACILITY_KINDS", "ITEM_STATUSES", "ORDER_STATUSES", "PARENT_LEVEL", "PAYMENT_STATUSES", "UNIT_LEVELS",
    "AttributeSchema", "AuditLog", "Category", "Country", "District", "Employee", "EmployeeUnit", "MessagingAccount",
    "Notification", "Order", "OrderCounter", "OrderItem", "OrgUnit", "OrgUnitClosure", "OtpChallenge", "OutboxMessage",
    "Patient", "Payment", "Region", "ResultDocument", "ResultTemplate", "Role", "ServiceType", "ServiceUnitSetting",
    "Session", "SmsTemplate", "StoredFile", "TelegramChatPref", "TelegramLink", "TemplateAsset", "TemplateUnit",
    "empty_progress",
]
