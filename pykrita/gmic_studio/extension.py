"""Menu actions (Tools > Scripts) so the plugin is usable without the docker open."""
from krita import Extension

from .studio import get_studio


class StudioExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)

    def setup(self):
        get_studio().refresh()

    def createActions(self, window):
        repeat = window.createAction("gmic_studio_repeat", "Repeat Last G'MIC Studio Filter", "tools/scripts")
        repeat.triggered.connect(lambda _checked=False: get_studio().repeat_last())
