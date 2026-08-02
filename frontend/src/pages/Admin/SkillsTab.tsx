import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Form, Input, Modal, Space, Table, Tag, Upload, message } from 'antd'
import { InboxOutlined, PlusOutlined, SyncOutlined, UploadOutlined } from '@ant-design/icons'
import { useState } from 'react'
import client from '../../api/client'

type SkillVersion = { id: string; version_no: number; status: string; checksum: string; manifest_name: string; manifest_description: string; validation_error?: string }
type Skill = { id: string; logical_name: string; name: string; description: string; status: string; version: number; current_version?: SkillVersion }
const headers = () => ({ 'Idempotency-Key': crypto.randomUUID() })

export default function SkillsTab() {
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<Skill | null>(null)
  const [packageFile, setPackageFile] = useState<File>()

  const skills = useQuery({ queryKey: ['skills'], queryFn: () => client.get('/skills').then(r => r.data.items as Skill[]) })
  const versions = useQuery({
    queryKey: ['skill-versions', selected?.id],
    queryFn: () => client.get(`/skills/${selected!.id}/versions`).then(r => r.data.items as SkillVersion[]),
    enabled: !!selected,
  })
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['skills'] })
    if (selected) queryClient.invalidateQueries({ queryKey: ['skill-versions', selected.id] })
  }
  const create = useMutation({
    mutationFn: ({ logical_name, file }: { logical_name: string; file: File }) => {
      const data = new FormData(); data.append('logical_name', logical_name); data.append('package', file)
      return client.post('/skills', data, { headers: { ...headers(), 'Content-Type': 'multipart/form-data' } })
    },
    onSuccess: () => { message.success('Skill 包已上传'); setOpen(false); setPackageFile(undefined); form.resetFields(); refresh() },
    onError: (error: any) => message.error(error?.response?.data?.error?.message || '上传失败'),
  })
  const uploadVersion = useMutation({
    mutationFn: (file: File) => { const data = new FormData(); data.append('package', file); return client.post(`/skills/${selected!.id}/versions`, data, { headers: { ...headers(), 'Content-Type': 'multipart/form-data' } }) },
    onSuccess: () => { message.success('新版本已上传'); refresh() },
  })
  const publish = useMutation({
    mutationFn: (version: SkillVersion) => client.post(`/skills/${selected!.id}/versions/${version.id}/publish`, {}, { headers: headers() }),
    onSuccess: () => { message.success('版本已发布，新 Runner 将解析此 checksum'); refresh() },
  })
  const sync = useMutation({
    mutationFn: (skill: Skill) => client.post(`/skills/${skill.id}/sync`, {}, { headers: headers() }),
    onSuccess: () => message.success('共享只读 Skill Workspace 已同步'),
    onError: (error: any) => message.error(error?.response?.data?.error?.code || '同步失败'),
  })

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>上传 Skill 包</Button>
      </div>
      <Table rowKey="id" loading={skills.isLoading} dataSource={skills.data || []} columns={[
        { title: '逻辑标识', dataIndex: 'logical_name' }, { title: '名称', dataIndex: 'name' },
        { title: '描述', dataIndex: 'description', ellipsis: true },
        { title: '当前版本', render: (_, skill: Skill) => skill.current_version ? <Tag color="success">v{skill.current_version.version_no}</Tag> : <Tag>未发布</Tag> },
        { title: 'Checksum', render: (_, skill: Skill) => skill.current_version?.checksum?.slice(0, 12) || '—' },
        { title: '操作', render: (_, skill: Skill) => <Space><Button size="small" onClick={() => setSelected(skill)}>版本</Button><Button size="small" icon={<SyncOutlined />} disabled={!skill.current_version} onClick={() => sync.mutate(skill)}>同步</Button></Space> },
      ]} />
      <Modal title="上传通用 Skill 包" open={open} onCancel={() => setOpen(false)} onOk={() => form.submit()} confirmLoading={create.isPending}>
        <Form form={form} layout="vertical" onFinish={values => packageFile && create.mutate({ logical_name: values.logical_name, file: packageFile })}>
          <Form.Item name="logical_name" label="逻辑标识" rules={[{ required: true }]}><Input placeholder="professional_writer" /></Form.Item>
          <Form.Item label="包文件" required><Upload.Dragger accept=".zip,.tar.gz,.tgz" maxCount={1} beforeUpload={file => { setPackageFile(file); return false }} onRemove={() => setPackageFile(undefined)}><p><InboxOutlined style={{ fontSize: 28 }} /></p><p>拖入包含根目录 SKILL.md 的 zip 或 tar.gz</p></Upload.Dragger></Form.Item>
        </Form>
      </Modal>
      <Modal title={`${selected?.name || ''} · 版本`} open={!!selected} onCancel={() => setSelected(null)} footer={null} width={820}>
        <Upload accept=".zip,.tar.gz,.tgz" showUploadList={false} beforeUpload={file => { uploadVersion.mutate(file); return false }}><Button icon={<UploadOutlined />} loading={uploadVersion.isPending}>上传新版本</Button></Upload>
        <Table style={{ marginTop: 12 }} rowKey="id" loading={versions.isLoading} dataSource={versions.data || []} pagination={false} columns={[
          { title: '版本', dataIndex: 'version_no', render: value => `v${value}` },
          { title: '状态', dataIndex: 'status', render: value => <Tag color={value === 'published' ? 'success' : value === 'rejected' ? 'error' : 'processing'}>{value}</Tag> },
          { title: 'Checksum', dataIndex: 'checksum', render: value => value.slice(0, 16) },
          { title: '操作', render: (_, version: SkillVersion) => <Button size="small" type="primary" disabled={version.status === 'published' || version.status === 'deprecated'} loading={publish.isPending} onClick={() => publish.mutate(version)}>发布</Button> },
        ]} />
      </Modal>
    </>
  )
}
