import raylib_nb as rl

from openpilot.system.ui.lib.application import gui_app, FontWeight, TextAlignment
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.label import gui_label


class PlaceholderApp(Widget):
  """Stand-in content for apps that aren't built yet."""

  def __init__(self, name: str):
    super().__init__()
    self._name = name

  def _render(self, rect: rl.Rectangle):
    big = gui_app.big_ui()
    title_size, sub_size = (110, 60) if big else (48, 30)
    mid = rect.y + rect.height / 2
    gui_label(rl.Rectangle(rect.x, mid - title_size * 1.1, rect.width, title_size * 1.2), self._name, title_size,
              rl.WHITE, FontWeight.BOLD, alignment=TextAlignment.CENTER)
    gui_label(rl.Rectangle(rect.x, mid + sub_size * 0.2, rect.width, sub_size * 1.2), "Coming soon", sub_size,
              rl.Color(160, 160, 160, 255), FontWeight.MEDIUM, alignment=TextAlignment.CENTER)
