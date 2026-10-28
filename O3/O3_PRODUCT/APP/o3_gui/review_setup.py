"""Compatibility entry point for the one human question Mail may need."""
from .workspace_editor import SharedSettings


class ReviewSetup(SharedSettings):
    """Mail is ready by default; setup only confirms the user's addresses."""
    def __init__(self, w):
        self.w = w
        super().__init__(w.km, w)
