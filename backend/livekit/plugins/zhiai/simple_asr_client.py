#!/usr/bin/env python3
"""
简单的ASR语音识别脚本 - 小白学习版
基于FunASR的Triton客户端代码
"""

import argparse
import os
import tritonclient.grpc as grpcclient
import numpy as np
import soundfile as sf
from tritonclient.utils import np_to_triton_dtype
import re
import time
import logging
from typing import Union, BinaryIO, Tuple

# 创建logger
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)  # 设置日志级别为DEBUG，保证所有级别都会被处理

# 创建控制台输出handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)  # 控制台输出的最低日志级别也是DEBUG

# 创建formatter，定义日志显示格式
formatter = logging.Formatter('[%(asctime)s] - %(levelname)s - %(message)s')

# 给handler设置formatter
console_handler.setFormatter(formatter)

# 给logger添加handler
logger.addHandler(console_handler)


class SimpleASRClient:
    """简单的ASR客户端类"""
    
    def __init__(self, server_url="localhost:8001", model_name="sensevoice"):
        """
        初始化ASR客户端
        
        Args:
            server_url: Triton服务器地址，格式为 "地址:端口"
            model_name: 模型名称
        """
        self.server_url = server_url
        self.model_name = model_name
        self.triton_client = None

    def connect(self):
        """连接到Triton服务器，建立grpc连接，保持连接周期和websocket一致"""
        try:
            print(f"正在连接到服务器: {self.server_url}")
            self.triton_client = grpcclient.InferenceServerClient(
                url=self.server_url,
                verbose=False
            )
            # 检查服务器是否就绪
            if not self.triton_client.is_server_ready():
                print("错误: 服务器未就绪")
                self.triton_client = None
                return False

            print("✅ 成功连接到服务器!")
            return True

        except Exception as e:
            print(f"❌ 连接失败: {e}")
            self.triton_client = None
            return False

    def is_connected(self):
        """判断grpc客户端是否连接"""
        return self.triton_client is not None

    def close(self):
        """关闭grpc连接，释放资源。调用方(如websocket断开时)调用"""
        if self.triton_client:
            self.triton_client.close()
            self.triton_client = None
            print("已关闭服务器连接")

    def load_audio(self, audio_path: Union[str, BinaryIO, np.ndarray]) -> Tuple:
        """
        加载音频文件

        Args:
            audio_path: 音频文件路径或二进制文件对象

        Returns:
            waveform: 音频波形数据
            sample_rate: 采样率
        """
        
        # 检查输入类型，处理文件路径或二进制文件对象
        if isinstance(audio_path, np.ndarray):
            waveform = audio_path
            sample_rate = 16000
        elif isinstance(audio_path, str):
            print(f"正在加载音频文件: {audio_path}")
            if not os.path.exists(audio_path):
                raise FileNotFoundError(f"音频文件不存在: {audio_path}")
            # 读取音频文件
            waveform, sample_rate = sf.read(audio_path)
        else:
            # 这里假设是 BinaryIO 类型（如 BytesIO）
            # 读取音频文件
            if audio_path.name.endswith(".mp3"):
                waveform, sample_rate = sf.read(audio_path.name)
            else:
                waveform, sample_rate = sf.read(audio_path)
        
        # 检查采样率，如果不是16kHz则转换
        if sample_rate != 16000:
            print(f"⚠️  当前采样率: {sample_rate}Hz，正在转换为16kHz...")
            from scipy.signal import resample
            num_samples = int(len(waveform) * (16000 / sample_rate))
            waveform = resample(waveform, num_samples)
            sample_rate = 16000
        
        duration = len(waveform) / sample_rate
        print(f"✅ 音频加载成功:")
        print(f"   - 时长: {duration:.2f}秒")
        print(f"   - 采样率: {sample_rate}Hz")
        print(f"   - 采样点数: {len(waveform)}")
        
        return waveform, sample_rate
    
    def recognize(self, audio_path):
        """
        识别音频文件
        
        Args:
            audio_path: 音频文件路径或音频数据 (np.ndarray或者路径)
            
        Returns:
            text: 识别出的文本
        """
        try:
            # 这里不再进行连接和关闭，连接保持由调用方控制，解决grpc和websocket连接周期一致问题
            if not self.is_connected():
                print("❌ 尚未连接服务器，请先调用 connect()")
                return None
            
            # 加载音频
            waveform, sample_rate = self.load_audio(audio_path)
            
            # 准备输入数据
            print("正在准备输入数据...")

            # 将音频数据重塑为(1, samples)的格式
            input_wav = waveform.reshape(1, -1).astype(np.float32)
            input_length = np.array([[len(waveform)]], dtype=np.int32)

            print(f"   - 音频数据形状: {input_wav.shape}")
            print(f"   - 长度数据: {input_length}")

            language = np.zeros((1, 1), dtype=np.int32)
            text_norm = np.full((1, 1), 15, dtype=np.int32)

            # 创建输入张量
            inputs = [
                grpcclient.InferInput("WAV", input_wav.shape, "FP32"),
                grpcclient.InferInput("WAV_LENS", input_length.shape, "INT32"),
            ]
            # 设置输入数据
            inputs[0].set_data_from_numpy(input_wav)
            inputs[1].set_data_from_numpy(input_length)

            if self.model_name == "sensevoice":
                inputs.extend([
                    grpcclient.InferInput("LANGUAGE", language.shape, np_to_triton_dtype(np.int32)),
                    grpcclient.InferInput("TEXT_NORM", text_norm.shape,np_to_triton_dtype(np.int32))
                ])
                inputs[2].set_data_from_numpy(language)
                inputs[3].set_data_from_numpy(text_norm)
            
            # 设置输出
            outputs = [grpcclient.InferRequestedOutput("TRANSCRIPTS")]

            # 发送推理请求
            print("正在发送ASR识别请求...")

            start = time.time()
            response = self.triton_client.infer(
                self.model_name,
                inputs,
                request_id="simple_asr_test",
                outputs=outputs
            )

            print("✅ 推理完成!")

            # 获取识别结果
            decoding_results_batch = response.as_numpy("TRANSCRIPTS")
            logger.info(f"识别结果: {decoding_results_batch}")

            results = []
            for i, decoding_results in enumerate(decoding_results_batch):
                if isinstance(decoding_results, np.ndarray):
                    decoding_results = b" ".join(decoding_results).decode("utf-8")
                else:
                    # For wenet
                    decoding_results = decoding_results.decode("utf-8")
                # remove special tokens in sensevoice results e.g. <|zh|><|NEUTRAL|><|Speech|><|woitn|>大学生利用漏洞免费吃肯德基祸刑
                # <|*|>, using re
                decoding_results = re.sub(r"<\|.*?\|>", "", decoding_results)
                results.append(decoding_results)
            end = time.time()
            ## 使用logging记录打印end - start的毫秒单位, 保留小数点1位数
            logger.info(f"识别耗时：{(end - start) * 1000:.1f} ms")

            logger.info(f"最终识别结果：{results}")
            return results[0] if len(results) > 0 else ""

        except Exception as e:
            print(f"❌ 识别过程中出现错误: {e}")
            import traceback
            traceback.print_exc()
            return None


# python simple_asr.py \
#     --audio-file /path/to/your/audio.wav \
#     --server-url localhost:8001 \
#     --model-name attention_rescoring
def main():
    """主函数"""
    # 创建命令行参数解析器
    parser = argparse.ArgumentParser(
        description="简单的ASR语音识别工具 - 小白学习版",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    # 添加参数
    parser.add_argument(
        "--audio-file", "-a", 
        # default="/data/disk1/lw_agent/FunASR/runtime/triton_gpu/client/man_1_talk_0.95.wav",
        default="/data/disk1/lw_agent/FunASR/runtime/triton_gpu/client/chinese_audio_0.mp3",
        # default="/data/disk1/lw_agent/FunASR/runtime/triton_gpu/client/4_36_immersion.mp3",
        help="音频文件路径 (支持wav, flac等格式)"
    )
    
    parser.add_argument(
        "--server-url", "-u", 
        default="localhost:8001", 
        help="Triton服务器地址 (默认: localhost:8001)"
    )
    
    parser.add_argument(
        "--model-name", "-m", 
        default="infer_pipeline", 
        choices=["attention_rescoring", "streaming_wenet", "infer_pipeline"],
        help="模型名称 (默认: sensevoice)"
    )
    
    # 解析参数
    args = parser.parse_args()
    
    # 打印欢迎信息
    print("🎤 欢迎使用简单ASR语音识别工具!")
    print("=" * 50)
    print(f"📁 音频文件: {args.audio_file}")
    print(f"🌐 服务器地址: {args.server_url}")
    print(f"🤖 模型名称: {args.model_name}")
    print("=" * 50)
    
    # 创建ASR客户端
    asr_client = SimpleASRClient(
        server_url=args.server_url,
        model_name=args.model_name
    )

    # 先连接服务器
    if not asr_client.connect():
        print("❌ 无法连接到服务器，程序退出")
        return

    # 执行识别
    print("\n🚀 开始语音识别...")
    result = asr_client.recognize(args.audio_file)

    # 输出结果
    print("\n" + "=" * 50)
    print("📝 识别结果:")
    print("=" * 50)
    
    if result:
        print(f"✅ 识别成功!")
        print(f"🎯 识别文本: {result}")
    else:
        print("❌ 识别失败!")

    print("=" * 50)

    # 结束关闭连接
    asr_client.close()

if __name__ == "__main__":
    main()
