from dataclasses import dataclass
from .models import ProjectModelConfig
@dataclass(frozen=True)
class ModelRoute: provider:str; base_url:str; model:str
class ModelRouter:
    def route(self,config:ProjectModelConfig|None,settings,role:str)->ModelRoute:
        field={"CHARACTER":"character_model","WORLD":"world_model","DIRECTOR":"director_model","REPAIR":"repair_model","WRITER":"writer_model","CRITIC":"critic_model"}[role]
        model=getattr(config,field) if config and getattr(config,field) else (settings.ai_character_model if role=="CHARACTER" else settings.ai_world_model)
        return ModelRoute(config.provider if config and config.provider else settings.ai_provider,config.base_url if config and config.base_url else settings.ai_base_url,model)
