from typing import AsyncGenerator, Optional, List, Union
import logging
from maga_transformer.config.generate_config import GenerateConfig
from maga_transformer.models.base_model import GenerateOutputs
from maga_transformer.openai.renderers.qwen_agent_renderer import QwenAgentRenderer
from maga_transformer.tokenizer.tokenization_qwen import QWenTokenizer
from transformers import Qwen2Tokenizer
from maga_transformer.openai.api_datatype import (
    ChatCompletionRequest,
    DeltaMessage,
    FinisheReason,
)
from maga_transformer.openai.renderers.custom_renderer import (
    StreamResponseObject,
    RenderedInputs,
    StreamStatus,
)
from maga_transformer.openai.renderer_factory_register import register_renderer
from maga_transformer.openai.renderers.qwen_agent.utils.tool_function_converter import (
    convert_tool_to_function_request,
    convert_function_to_tool_response,
)

QwenTokenizerTypes = Union[QWenTokenizer, Qwen2Tokenizer]

class QwenAgentToolRenderer(QwenAgentRenderer):

    # override
    def render_chat(self, request: ChatCompletionRequest) -> RenderedInputs:
        # 转换request从tool协议到function协议
        return super().render_chat(self._convert_to_function_request(request))

    # override
    def _parse_function_response(self, response: str) -> Optional[DeltaMessage]:
        delta_message = super()._parse_function_response(response)
        if delta_message and delta_message.function_call:
            # 转换function_call成tool_call
            tool_delta_dict = convert_function_to_tool_response(
                delta_message.model_dump(exclude_none=True)
            )
            logging.info(
                f"convert {delta_message.model_dump_json(exclude_none=True)} to {tool_delta_dict}"
            )
            return DeltaMessage(**tool_delta_dict)

        return delta_message

    async def render_response_stream(
        self,
        output_generator: AsyncGenerator[GenerateOutputs, None],
        request: ChatCompletionRequest,
        generate_config: GenerateConfig,
    ) -> AsyncGenerator[StreamResponseObject, None]:
        async for stream_response_object in super().render_response_stream(
            output_generator,
            self._convert_to_function_request(request),
            generate_config,
        ):
            if (
                stream_response_object.choices[0].finish_reason
                == FinisheReason.function_call
            ):
                stream_response_object.choices[0].finish_reason = (
                    FinisheReason.tool_calls
                )
            yield stream_response_object

    def _convert_to_function_request(
        self, tool_request: ChatCompletionRequest
    ) -> ChatCompletionRequest:
        function_request_dict = convert_tool_to_function_request(
            tool_request.model_dump(exclude_none=True)
        )
        function_request = ChatCompletionRequest(**function_request_dict)
        return function_request


register_renderer("qwen_agent_tool", QwenAgentToolRenderer)
