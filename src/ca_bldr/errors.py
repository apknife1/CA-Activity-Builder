class TableResizeError(RuntimeError):
    """Raised when an interactive table cannot be resized to the required rows/cols."""
    pass

class FieldPropertiesSidebarTimeout(RuntimeError):
    """Raised when the field properties sidebar cannot be opened reliably."""
    pass