# src/ca_bldr/config_builder.py

from __future__ import annotations

from dataclasses import fields
from typing import Dict, Type, Any

from .field_configs import (
    BaseFieldConfig,
    ParagraphConfig,
    QuestionFieldConfig,
    LongAnswerConfig,
    ShortAnswerConfig,
    FileUploadConfig,
    SignatureConfig,
    DatePickerConfig,
    SingleChoiceConfig,
    TableConfig,
    TableCellConfig,
)

from .spec_reader import FieldInstruction

# Map spec "type" (and/or question_type) to a config dataclass.
FIELD_TYPE_TO_CONFIG: Dict[str, Type[BaseFieldConfig]] = {
    "paragraph": ParagraphConfig,
    "long_answer": LongAnswerConfig,
    "short_answer": ShortAnswerConfig,
    "file_upload": FileUploadConfig,
    "interactive_table": TableConfig,
    "signature": SignatureConfig,
    "date_field": DatePickerConfig,
    "single_choice": SingleChoiceConfig,
    # future: "mcq_single": McqSingleConfig, etc.
}

def _infer_config_class(component: Dict[str, Any], field_key_fallback: str | None = None) -> Type[BaseFieldConfig]:
    """
    Decide which config dataclass to use for this component.

    Primary: 'type' key in the YAML.
    Secondary: fall back to 'question_type' or to the passed field_key_fallback if needed for legacy specs.
    """
    field_type = component.get("type")
    question_type = component.get("question_type")

    # 1) Direct mapping using "type" (preferred going forward)
    if field_type in FIELD_TYPE_TO_CONFIG:
        return FIELD_TYPE_TO_CONFIG[field_type]

    # 2) Transitional support: infer from question_type if present
    #    This lets you have older specs while you migrate to a unified "type".
    if question_type:
        if question_type == "long_answer":
            return LongAnswerConfig
        if question_type == "short_answer":
            return ShortAnswerConfig
        if question_type == "file_upload":
            return FileUploadConfig
        # add other question_type mappings here as you introduce them

    # 3) Fallback: use field_key from FieldInstruction if provided
    if field_key_fallback and field_key_fallback in FIELD_TYPE_TO_CONFIG:
        return FIELD_TYPE_TO_CONFIG[field_key_fallback]
    
    raise ValueError(
        f"Cannot infer config class for component; "
        f"got type={field_type!r}, question_type={question_type!r}, , fallback={field_key_fallback!r}"
    )

def _parse_cell_overrides(raw: dict) -> dict[tuple[int, int], TableCellConfig]:
    parsed: dict[tuple[int, int], TableCellConfig] = {}
    for k, v in (raw or {}).items():
        if isinstance(k, tuple) and len(k) == 2:
            r, c = int(k[0]), int(k[1])
        if isinstance(k, str) and "," in k:
            r_s, c_s = k.split(",", 1)
            r, c = int(r_s.strip()), int(c_s.strip())
        else:
            continue

        # ✅ Wrap raw string into TableCellConfig
        parsed[(r, c)] = TableCellConfig(text=(v or "") if isinstance(v, str) else str(v))

    return parsed

def build_field_config(field: FieldInstruction) -> BaseFieldConfig:
    """
    Central factory: take a spec component dict and return the matching
    field config dataclass instance.

    - Figures out which config class to use (ParagraphConfig, LongAnswerConfig, etc.)
    - Filters the dict so we only pass fields that the dataclass actually defines.
    - Leaves higher-level spec keys (question_id, mapping refs, etc.) to other code.
    """
    component = field.raw_component or {}
    field_key_fallback = field.field_key

    ConfigClass = _infer_config_class(component, field_key_fallback=field_key_fallback)

    # Collect valid field names for this config class
    valid_field_names = {f.name for f in fields(ConfigClass)}

    # Filter the component dict down to the keys that actually belong on the config
    config_kwargs = {
        key: value
        for key, value in component.items()
        if key in valid_field_names
    }

    # ✅ Normalize table cell overrides: allow YAML-friendly "r,c" keys
    if ConfigClass is TableConfig and "cell_overrides" in config_kwargs:
        config_kwargs["cell_overrides"] = _parse_cell_overrides(config_kwargs["cell_overrides"])

    return ConfigClass(**config_kwargs)
