from krita import Krita, DockWidgetFactory, DockWidgetFactoryBase

from .docker import StudioDocker
from .extension import StudioExtension

Krita.instance().addExtension(StudioExtension(Krita.instance()))
Krita.instance().addDockWidgetFactory(
    DockWidgetFactory("gmic_studio_docker", DockWidgetFactoryBase.DockRight, StudioDocker))
