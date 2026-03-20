"""测试配置"""

import os
import sys
import tempfile
import shutil
import pytest

# 将 mock_bt 注册为宝塔模块
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MOCK_BT_DIR = os.path.join(TESTS_DIR, 'mock_bt')

# 注入 mock 模块
sys.modules['panelSite'] = __import__('mock_bt.panelSite', fromlist=['panelSite'])
sys.modules['public'] = __import__('mock_bt.public', fromlist=['public'])

# 添加 src 到路径
SRC_DIR = os.path.join(os.path.dirname(TESTS_DIR), 'src')
sys.path.insert(0, SRC_DIR)


@pytest.fixture
def tmp_data_dir():
    """创建临时数据目录"""
    d = tempfile.mkdtemp(prefix='sslbt_test_')
    os.makedirs(os.path.join(d, 'logs'), exist_ok=True)
    os.makedirs(os.path.join(d, 'pending-keys'), exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def config_manager(tmp_data_dir):
    from lib.config import ConfigManager
    return ConfigManager(tmp_data_dir)


@pytest.fixture
def logger(tmp_data_dir):
    from lib.logger import Logger
    return Logger(os.path.join(tmp_data_dir, 'logs'))


# 测试用证书（自签名）
SAMPLE_CERT = """-----BEGIN CERTIFICATE-----
MIICpDCCAYwCCQDU+pGm3oNMGzANBgkqhkiG9w0BAQsFADAUMRIwEAYDVQQDDAls
b2NhbGhvc3QwHhcNMjUwMTAxMDAwMDAwWhcNMjYwMTAxMDAwMDAwWjAUMRIwEAYD
VQQDDAlsb2NhbGhvc3QwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQC7
7JhdC7yGGBkxMOQAyBMGbJPVWJYw/QHwxSCZErXRo0cMaDNyjPsxGqnhOqKKmGhQ
HBFjBqWzTZOCJqHDRHSc/KBWfVKH3HjnU7K3JVoxsChXB9kGGLFBkxkwaM5qgyJx
1kNMMABbg5gTfAQHQ3vHqNTgPoxiLGhCPuJVnMHJVGxqqT8HNQE1RxrJK/UiQerR
YRdvoMSHeR+JgOKFV2YpN9GgsOJkFqW4RkMiLo4eFJRzVfkVJTHHwmiXSbCHETVc
TwdkVMuUQjxrmBJBmGAlI5+eAOhVX9qJoBlzLCVPxLiiVTpdOlJMUxLgnwl/IFWF
rBESGu6kVR3RqG95AZ7hAgMBAAEwDQYJKoZIhvcNAQELBQADggEBADlY2C8PEIGR
bcXYMBpFBFklVo3F+EB4ggwPmlNFDS8RJEyTO6KalWVRmWOK0ThhMQCtQM7CYXQT
L5VT6PakZ7URq4NlN0JxhEkP+qIOjDH7MIjOYLx2yZDbjaEUu7p+6Dt84CyQBIq
+8wVpDqk5laj/cNTZVX8efO9mVvjT2nONhmYKBENm+TkFwJjbFhSfZ2Ajfl6RMGA
gRNLy0VbXOHGnb8zOKQf3MwNpK1UPO4r6p6+LqhJT1dcSe9yLlMF3Y3GKx03uFD
ZMJAC6OQFb/MqPPjRAVcmNVCRyzqv7VnPhmMQQz1LZ/pGQmhI7p6dI5P2aHZ8z2c
AQHJ4LNE3Cc=
-----END CERTIFICATE-----"""

SAMPLE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAu+yYXQu8hhgZMTDkAMgTBmyT1ViWMP0B8MUgmRK10aNHDGgz
coz7MRqp4TqiiphoUBwRYwals02TgiaZw0R0nPygVn1Sh9x451OytyVaMbAoVwfZBh
ixQZMZMGjOaoMicdZDTDAAW4OYE3wEB0N7x6jU4D6MYixoQj7iVZzByVRsaqk/BzU
BNUcaySv1IkHq0WEXb6DElnkfiYDihVdmKTfRoLDiZBaluEZDIi6OHhSUc1X5FSUx
x8Jol0mwhxE1XE8HZFTLlEI8a5gSQZhgJSOfngDoVV/aiaAZcywlT8S4olU6XTpST
FMS4J8JfyBVhawREhrupFUd0ahveQGe4QIDAQABAoIBAFqfEz2r3WAG0mCqdh7YRoS
Sl+r6EAEaGj7of3IyfeE0gx1yzKJh6ZJKWDZ2+5fHe5LClDayXGFTDLIfrz7RShke
KBZqJ4aJYUrdcbnL9dDFaAkFfQLdQqFR3clSKf0QBDj0FzPMqQ+NKn+/IwXfJIDEB
E0sSrzETm6PoqpNmwbG1vSgdAixynxXgfvbqn12J5+GuJqFR3JvKAQ5OMkJM5e+hJ
ULGNYsR0jJE7xkAXbxYJk/UAlj1aA3C0vJQb0xME7T8DKIRwRHtFCjc1qDT1dnE4B
QWWrbKKeBisHY8xyJFrUVPLSNiaBuq1IjGBBYFNECgYEA3O7F9Pmfj1BI7OVKLz2N
muYaJGVljxdCHSi+K0Ss/IH/3IqGWNXdUMkdGYab1JWctLM1bqSIDUkl7B+NoIxP7
QZxOuxhOvMjXAEeqHS4EDJzHOJCJ0Y7G8CxqJ3b5FR3BRXM0pRILPRXA2rWIz1LjM
mgfNIMFMo7yCDo35RlvECgYEA27QhutdYlBT3GSST8XR6MAmTrBaMNOQQ5A9IM7+H
0MBi4cLAH3Ij6P8A+2Bq1DSNFGFlbxH/MeZ2NQFnVHG5J2rxAkgJh8kCEal/VQgdm
BYiWoWZPaXNhPJ0bORQJJNYbxliUA7IhR3pcXTE0k2b9R3sSlz5O1R0w99P1qlGXEC
gYBzjy2S+PJpC5VL/U3l03LBbdHlufgWS9W1k+dfFLvH5dXKH6X4NcaRdWlz7S2L2
H0BPKKxS0PDqVYh/tLiugvW3+WhPmnJXgSCFP+fKDqjfQN/aPE3gZ9LDqYysbhTxP
OLO6ThrVqLfB7mPsHq3t2Y4KQNRoQF3KcNx/BQFKQQKBgBshfenVGYGPTVLcmtaU6
qygEW/SmwEuMhmPXtsmJijMqVfPjQB0aJ+Ko6Ql8YIAZ+2DvqUVJP8n3UYBBNwf4F
C7bWQJvCxJ4DUwZKrVMn5FAlXF8KCBi16l2y1cTqjQ1MtXnJLdAF/0WI4HjNQ7FQM
N8jqctGZslNWYlyRahKBAoGBAJ9uL/yCXmJYNVyqcCHe1fTqGLtyhLG6L2sVz+nW9
T9dv6sCU8s/X/w2KXCdTAWJ3QEGmBViAw9hN8E4SPYJknyHJlbXEtPy+ZuNpmqfJz
6cSEPTsK+Z7VvQPxhqW4tjvKSqhOWkbrGBf8X5dMsPVj3YMCuaDdVJjHiC3x80R
-----END RSA PRIVATE KEY-----"""
