import logging
import json
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from selenium.common.exceptions import TimeoutException

from .ca_bldr.session import CASession, LoginError

from .ca_bldr.activity_registry import ActivityRegistry
from .ca_bldr.activity_builder import CAActivityBuilder  
from .ca_bldr.activity_editor import ActivityEditor
from .ca_bldr.activity_deleter import ActivityDeleter
from .ca_bldr.activity_sections import ActivitySections
from .ca_bldr.spec_reader import SpecReader, FieldInstruction, ActivityInstruction
from .ca_bldr.context import AppContext
from .ca_bldr.controller import ActivityBuildController

from .ca_bldr.activity_snapshot import build_registry_from_current_activity

from .ca_bldr.field_types import FIELD_TYPES
from .ca_bldr.field_configs import TableConfig, QuestionFieldConfig, ParagraphConfig, SignatureConfig, DatePickerConfig
from .ca_bldr.config_builder import build_field_config

from dataclasses import dataclass

def main():

    logger = setup_logging()

    session = CASession(logger)
    registry = ActivityRegistry()

    try:
        session.login()
    except LoginError as e:
        logger.error(str(e))
        # Stop here – don’t try to build activities
        return 1
    
    # Build shared components ONCE
    deleter = ActivityDeleter(session, registry=registry)
    sections = ActivitySections(session, registry=registry, deleter=deleter)
    editor = ActivityEditor(session, registry=registry)
    builder = CAActivityBuilder(session, sections=sections, editor=editor, registry=registry)
    reader = SpecReader(logger)

    ctx = AppContext(
        logger=logger,
        session=session,
        registry=registry,
        sections=sections,
        builder=builder,
        editor=editor,
        deleter=deleter,
        reader=reader,
    )

    controller = ActivityBuildController(ctx)

    try:
        controller.control_process()
        
    except Exception as e:
        logger.error("Well that seems to have failed. Message %r.", e)
    finally:
        session.close()

    return 0

def setup_logging(verbose_console: bool = False):
    logger = logging.getLogger("ca_bldr")
    logger.setLevel(logging.DEBUG)  # emit everything; handlers will filter

    # Clear existing handlers if this is called multiple times
    logger.handlers.clear()
    logger.propagate = False  # don't double-log via root

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)

    # --- Console: WARNING (or DEBUG if verbose_console=True) ---
    console_handler = logging.StreamHandler()
    console_level = logging.DEBUG if verbose_console else logging.WARNING
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)

    # --- File: DEBUG, truncated each run ---
    file_handler = logging.FileHandler("ca_activity_builder.log", mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.name = "default_file"

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger

if __name__ == "__main__":
    main()
