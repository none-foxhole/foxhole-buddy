"""Discord UI views, grouped by feature.

Split into per-feature modules; everything is re-exported here so existing
``from foxhole_buddy.ui.views import X`` imports keep working unchanged.
"""

from foxhole_buddy.ui.views.factory import FactoryAlarmCardView, FactoryMenuView
from foxhole_buddy.ui.views.logistics import (
    CategorySelect,
    CategorySelectView,
    ItemSelect,
    ItemSelectView,
    LogisticsActionsView,
    LogisticsRequestCardView,
    SubcategorySelect,
    SubcategorySelectView,
)
from foxhole_buddy.ui.views.menu import MainMenuView
from foxhole_buddy.ui.views.regi import (
    AllyNetPanelView,
    AllyTransmitModal,
    RegiNetPanelView,
    TransmitModal,
)
from foxhole_buddy.ui.views.operations import (
    LinkLogisticsView,
    ManageLeadsView,
    OperationCardView,
    OperationsActionsView,
    SquadSignupView,
)
from foxhole_buddy.ui.views.setup import SetupView
from foxhole_buddy.ui.views.stockpile import (
    RefreshStockpileButton,
    StockpileActionsView,
    StockpileTypeView,
    StockpileView,
)
from foxhole_buddy.ui.views.war import WarReportSelect, WarReportSelectView, WarRoomView

__all__ = [
    "AllyNetPanelView",
    "AllyTransmitModal",
    "CategorySelect",
    "CategorySelectView",
    "FactoryAlarmCardView",
    "FactoryMenuView",
    "ItemSelect",
    "ItemSelectView",
    "LinkLogisticsView",
    "LogisticsActionsView",
    "LogisticsRequestCardView",
    "MainMenuView",
    "ManageLeadsView",
    "OperationCardView",
    "OperationsActionsView",
    "RefreshStockpileButton",
    "RegiNetPanelView",
    "SetupView",
    "TransmitModal",
    "SquadSignupView",
    "StockpileActionsView",
    "StockpileTypeView",
    "StockpileView",
    "SubcategorySelect",
    "SubcategorySelectView",
    "WarReportSelect",
    "WarReportSelectView",
    "WarRoomView",
]
