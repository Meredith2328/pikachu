import sys
import os
import unittest
import socket

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def occupied_port():
    """绑定并保持一个端口被占用。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    return port, s


class TestPortFallback(unittest.TestCase):
    def test_pet_embedded_bus_falls_back_when_port_taken(self):
        """桌宠内嵌总线遇到端口被非 pika 服务占用：回退随机端口，不崩溃。"""
        from pathlib import Path
        from pika.pet import PikaPet
        port_file = (Path(__file__).resolve().parent.parent
                     / "runtime" / "port")
        backup = None
        if port_file.exists():   # 产品会写这个文件；先挪走，测完恢复
            backup = port_file.with_suffix(".isolated-bak")
            port_file.replace(backup)
        port, blocker = occupied_port()
        try:
            pet = PikaPet(port=port)
            try:
                self.assertIsNotNone(pet.server)
                self.assertNotEqual(pet.server.port, port)
                self.assertTrue(pet.server.running)
            finally:
                pet._quit()
        finally:
            blocker.close()
            if backup is not None:
                backup.replace(port_file)   # 恢复原状（内容指向已死端口，
                # 留着会让依赖协商的测试随环境抖动）

    def test_external_bus_is_subscribed_not_embedded(self):
        """已有独立总线在跑时，桌宠订阅它而不是再开一个内嵌总线。"""
        from pika.bus import BusServer
        from pika.pet import PikaPet
        bus = BusServer(port=0).start()
        try:
            pet = PikaPet(port=bus.port)
            try:
                self.assertIsNone(pet.server)  # 不内嵌
                self.assertIsNotNone(pet.sse)  # 订阅外部
            finally:
                pet._quit()
        finally:
            bus.stop()


if __name__ == "__main__":
    unittest.main()
