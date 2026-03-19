from typing import Optional
from pydantic import BaseModel, Field, field_validator

class SpecInput(BaseModel):
    """
    Validation model for SpecEngine execute input.
    """
    requirement_text: str = Field(..., description="The requirement description for the spec project")
    task_id: Optional[str] = Field(None, description="Optional task ID")
    
    @field_validator('requirement_text')
    def validate_requirement_text(cls, v):
        if not v or not v.strip():
            raise ValueError("Requirement text cannot be empty")
        if len(v) > 50000:  # Reasonable upper limit
            raise ValueError("Requirement text is too long (max 50000 chars)")
        return v

class SpecConfig(BaseModel):
    """
    Validation model for SpecEngine configuration.
    """
    max_cycles: int = Field(default=10, ge=1, le=100)
    execution_timeout: int = Field(default=300, ge=30)
    
    @field_validator('max_cycles')
    def validate_max_cycles(cls, v):
        if v < 1:
            raise ValueError("max_cycles must be at least 1")
        return v
