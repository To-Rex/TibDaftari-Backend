"""All ORM models — import this module so Alembic and the app see every table."""

from app.infrastructure.db.models.catalog import (
    AttributeSchema,
    Category,
    ResultTemplate,
    ServiceType,
    StoredFile,
    TemplateAsset,
)
from app.infrastructure.db.models.messaging import AuditLog, Notification, OutboxMessage
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
from app.infrastructure.db.models.patient import District, Patient, Region, TelegramChatPref, TelegramLink
from app.infrastructure.db.models.tenant import Branch, Company, Employee, OtpChallenge, Role, Session

__all__ = [
    "ITEM_STATUSES", "ORDER_STATUSES", "PAYMENT_STATUSES",
    "AttributeSchema", "AuditLog", "Branch", "Category", "Company", "District", "Employee",
    "Notification", "Order", "OrderItem", "OtpChallenge", "OutboxMessage", "Patient", "Payment",
    "Region", "ResultDocument", "ResultTemplate", "Role", "ServiceType", "Session", "StoredFile",
    "TelegramChatPref", "TelegramLink", "TemplateAsset", "empty_progress",
]
