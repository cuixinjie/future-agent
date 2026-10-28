"""Direct arm shares the final structure but never consumes C's generated inputs."""

from future_agent.prompts.forecast import FORECAST_SYSTEM

DIRECT_SYSTEM = FORECAST_SYSTEM + "\n直接预测组：只使用提供的共享基础包，不检索，不读取其他组产物。"
