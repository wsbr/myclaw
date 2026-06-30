import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Table, Button, Form, Input, Modal, message } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { useState } from 'react'
import client from '../../api/client'
import PromptEditor from '../../components/PromptEditor'

export default function SkillsTab() {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['skills'],
    queryFn: () => client.get('/skills').then(r => r.data),
  })

  const createMutation = useMutation({
    mutationFn: (v: any) => client.post('/skills', v).then(r => r.data),
    onSuccess: () => { message.success('Skill 已创建'); qc.invalidateQueries({ queryKey: ['skills'] }); setOpen(false); form.resetFields() },
  })

  const columns = [
    { title: '名称', dataIndex: 'name', key: 'name' },
    { title: '显示名', dataIndex: 'display_name', key: 'display_name' },
    { title: '描述', dataIndex: 'description', key: 'description', ellipsis: true },
    { title: '版本', dataIndex: 'version', key: 'version' },
  ]

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>新建 Skill</Button>
      </div>
      <Table dataSource={data || []} columns={columns} rowKey="id" loading={isLoading} />
      <Modal title="新建 Skill" open={open} onCancel={() => setOpen(false)} onOk={() => form.submit()} width={700}>
        <Form form={form} onFinish={createMutation.mutate} layout="vertical">
          <Form.Item name="name" label="唯一标识" rules={[{ required: true }]}><Input placeholder="e.g. professional_writer" /></Form.Item>
          <Form.Item name="display_name" label="显示名" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label="描述" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="content" label="提示词内容" rules={[{ required: true }]}>
            <PromptEditor height={200} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
