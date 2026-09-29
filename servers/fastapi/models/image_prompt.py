from typing import Optional
from pydantic import BaseModel, Field, model_validator


class ImagePrompt(BaseModel):
    prompt: str
    theme_prompt: Optional[str] = None
    # Template image dimensions used for supported provider ratios or ComfyUI inputs.
    target_width: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    target_height: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_target_size(self) -> "ImagePrompt":
        if (self.target_width is None) != (self.target_height is None):
            raise ValueError("target_width and target_height must be supplied together")
        return self

    @property
    def target_size(self) -> tuple[float, float] | None:
        if self.target_width is None or self.target_height is None:
            return None
        return self.target_width, self.target_height

    def get_image_prompt(self, with_theme: bool = False) -> str:
        return f"{self.prompt}, {self.theme_prompt}" if with_theme else self.prompt
