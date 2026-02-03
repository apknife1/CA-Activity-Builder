# src/ca_bldr/context.py
from dataclasses import dataclass
import logging

from .session import CASession
from .activity_registry import ActivityRegistry
from .activity_sections import ActivitySections
from .activity_builder import CAActivityBuilder
from .activity_editor import ActivityEditor
from .activity_deleter import ActivityDeleter
from .spec_reader import SpecReader

@dataclass
class AppContext:
    logger: logging.Logger
    session: CASession
    registry: ActivityRegistry
    sections: ActivitySections
    builder: CAActivityBuilder
    editor: ActivityEditor
    deleter: ActivityDeleter
    reader: SpecReader