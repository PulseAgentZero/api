from pydantic import BaseModel


class CompleteOnboardingResponse(BaseModel):
    message: str
    onboarding_done: bool
