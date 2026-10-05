from enum import Enum


class ActionClass(str, Enum):
    SAFE = "safe"
    CONSEQUENTIAL = "consequential"
    DANGEROUS = "dangerous"
