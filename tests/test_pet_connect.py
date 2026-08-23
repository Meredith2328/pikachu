import sys
import os
import unittest
import socket

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import isolated_home


def occupied_port():
    """绑定并保持一个端口被占用。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    return port, s


class TestPortFallback(unittest.TestCase):
    def test_pet_embedded_bus_falls_back_when_port_taken(self):
        """桌宠内嵌总线遇到端口被别的服务占用：回退随机端口，不崩溃。

        隔离运行时目录，桌宠写的 port 文件落在临时目录里——不会留下一个
        指向已死端口的真实 port 文件，让依赖协商的其他测试随环境抖动。"""
        from pikapet.pet import PikaPet
        port, blocker = occupied_port()
        try:
            with isolated_home():
                pet = PikaPet(port=port)
                try:
                    self.assertIsNotNone(pet.server)
                    self.assertNotEqual(pet.server.port, port)
                    self.assertTrue(pet.server.running)
                finally:
                    pet._quit()
        finally:
            blocker.close()

    def test_external_bus_is_subscribed_not_embedded(self):
        """已有独立总线在跑时，桌宠订阅它而不是再开一个内嵌总线。"""
        from pikapet.bus import BusServer
        from pikapet.pet import PikaPet
        with isolated_home():
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
