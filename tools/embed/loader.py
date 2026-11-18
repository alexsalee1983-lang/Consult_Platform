"""加载向量模型。fastembed 内置列表里没有的模型在 CUSTOM 里登记（ONNX 文件来自模型官方仓库），
用于向量模型对比评测（决策记录 2026-11-18：bge-m3 与现用模型在测试机上对比）。"""
from fastembed import TextEmbedding
from fastembed.common.model_description import ModelSource, PoolingType

# 模型名 → (向量维度, 池化方式, ONNX 主文件, 附加文件)
CUSTOM = {
    "BAAI/bge-m3": (1024, PoolingType.CLS, "onnx/model.onnx", ["onnx/model.onnx_data"]),
}


def load(model: str, cache_dir: str = "/models") -> TextEmbedding:
    known = {m["model"].lower() for m in TextEmbedding.list_supported_models()}
    if model.lower() not in known and model in CUSTOM:
        dim, pooling, model_file, extra = CUSTOM[model]
        TextEmbedding.add_custom_model(model=model, pooling=pooling, normalization=True,
                                       sources=ModelSource(hf=model), dim=dim, model_file=model_file,
                                       additional_files=extra)
    return TextEmbedding(model, cache_dir=cache_dir)
