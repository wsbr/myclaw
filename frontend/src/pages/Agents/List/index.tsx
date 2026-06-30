import { useQuery } from '@tanstack/react-query'
import { Card, Button, Tag, Space, Typography, Empty, Spin, Row, Col } from 'antd'
import { PlusOutlined, RobotOutlined, SettingOutlined, MessageOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { agentsApi } from '../../../api/agents'

export default function AgentListPage() {
  const navigate = useNavigate()
  const { data: agents, isLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: agentsApi.list,
  })

  const modeColors: Record<string, string> = { chat: 'blue', plan: 'purple', group: 'green' }

  if (isLoading) return <Spin size="large" style={{ display: 'block', margin: '48px auto' }} />

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 24 }}>
        <Typography.Title level={4} style={{ margin: 0 }}>Agent 列表</Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/agents/new/config')}>
          新建 Agent
        </Button>
      </div>
      {agents?.length === 0 ? (
        <Empty description="暂无 Agent，点击右上角新建" />
      ) : (
        <Row gutter={[16, 16]}>
          {agents?.map((agent: any) => (
            <Col xs={24} sm={12} md={8} xl={6} key={agent.id}>
              <Card
                hoverable
                actions={[
                  <Button key="config" type="text" icon={<SettingOutlined />} onClick={() => navigate(`/agents/${agent.id}/config`)}>配置</Button>,
                  <Button key="chat" type="text" icon={<MessageOutlined />} onClick={() => navigate(`/agents/${agent.id}/chat`)}>对话</Button>,
                ]}
              >
                <Card.Meta
                  avatar={<RobotOutlined style={{ fontSize: 32, color: '#1677ff' }} />}
                  title={agent.name}
                  description={
                    <Space direction="vertical" size={4}>
                      <Typography.Text type="secondary" ellipsis style={{ maxWidth: 200 }}>
                        {agent.description}
                      </Typography.Text>
                      <Space>
                        {agent.modes?.map((m: string) => (
                          <Tag key={m} color={modeColors[m]}>{m}</Tag>
                        ))}
                        <Tag color={agent.status === 'active' ? 'success' : 'default'}>
                          {agent.status}
                        </Tag>
                      </Space>
                    </Space>
                  }
                />
              </Card>
            </Col>
          ))}
        </Row>
      )}
    </div>
  )
}
