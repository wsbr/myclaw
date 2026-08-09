import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd'
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  FileMarkdownOutlined,
  InboxOutlined,
  PlusOutlined,
  SaveOutlined,
  SearchOutlined,
  SyncOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { useEffect, useMemo, useState } from 'react'
import PromptEditor from '../../components/PromptEditor'
import client from '../../api/client'

type SkillVersion = {
  id: string
  version_no: number
  status: string
  checksum: string
  content_hash: string
  package_size?: number
  archive_type?: string
  manifest_name: string
  manifest_description: string
  validation_error?: string
  created_at?: string
  published_at?: string
}

type Skill = {
  id: string
  logical_name: string
  name: string
  description: string
  status: string
  version: number
  current_published_version_id?: string
  current_version?: SkillVersion
}

type SkillMarkdown = {
  skill_id: string
  version_id: string
  version_no: number
  status: string
  checksum: string
  files: Array<{ path: string; size: number }>
  path: string
  content: string
}

const headers = () => ({ 'Idempotency-Key': crypto.randomUUID() })

function formatBytes(value?: number) {
  if (value == null) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KiB`
  return `${(value / 1024 / 1024).toFixed(1)} MiB`
}

function statusTag(status: string) {
  const config: Record<string, { color: string; label: string }> = {
    published: { color: 'success', label: '已发布' },
    uploaded: { color: 'processing', label: '待发布' },
    validating: { color: 'warning', label: '校验中' },
    deprecated: { color: 'default', label: '已归档' },
    rejected: { color: 'error', label: '已拒绝' },
  }
  const current = config[status] || { color: 'default', label: status }
  return <Tag color={current.color}>{current.label}</Tag>
}

function apiError(error: any, fallback: string) {
  return error?.response?.data?.error?.message || error?.response?.data?.error?.code || fallback
}

export default function SkillsTab() {
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const [uploadOpen, setUploadOpen] = useState(false)
  const [packageFile, setPackageFile] = useState<File>()
  const [selectedSkillId, setSelectedSkillId] = useState('')
  const [selectedVersionId, setSelectedVersionId] = useState('')
  const [selectedPath, setSelectedPath] = useState('SKILL.md')
  const [editorContent, setEditorContent] = useState('')
  const [contentMode, setContentMode] = useState<'edit' | 'preview'>('edit')
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<'all' | 'published' | 'draft'>('all')

  const skills = useQuery({
    queryKey: ['skills'],
    queryFn: () => client.get('/skills').then(r => r.data.items as Skill[]),
  })
  const selectedSkill = useMemo(
    () => skills.data?.find(skill => skill.id === selectedSkillId) || null,
    [skills.data, selectedSkillId],
  )
  const versions = useQuery({
    queryKey: ['skill-versions', selectedSkillId],
    queryFn: () => client.get(`/skills/${selectedSkillId}/versions`).then(r => r.data.items as SkillVersion[]),
    enabled: !!selectedSkillId,
  })
  const sortedVersions = useMemo(
    () => [...(versions.data || [])].sort((a, b) => b.version_no - a.version_no),
    [versions.data],
  )
  const selectedVersion = sortedVersions.find(version => version.id === selectedVersionId) || null
  const markdown = useQuery({
    queryKey: ['skill-markdown', selectedSkillId, selectedVersionId, selectedPath],
    queryFn: () => client.get(
      `/skills/${selectedSkillId}/versions/${selectedVersionId}/markdown`,
      { params: { path: selectedPath } },
    ).then(r => r.data as SkillMarkdown),
    enabled: !!selectedSkillId && !!selectedVersionId,
    refetchOnWindowFocus: false,
  })

  useEffect(() => {
    if (!selectedSkillId && skills.data?.length) setSelectedSkillId(skills.data[0].id)
  }, [selectedSkillId, skills.data])

  useEffect(() => {
    setSelectedVersionId('')
    setSelectedPath('SKILL.md')
    setEditorContent('')
    setContentMode('edit')
  }, [selectedSkillId])

  useEffect(() => {
    if (!sortedVersions.length) return
    if (!selectedVersionId || !sortedVersions.some(version => version.id === selectedVersionId)) {
      const preferred = sortedVersions.find(version => version.id === selectedSkill?.current_published_version_id)
      setSelectedVersionId((preferred || sortedVersions[0]).id)
    }
  }, [selectedSkill?.current_published_version_id, selectedVersionId, sortedVersions])

  useEffect(() => {
    if (markdown.data) setEditorContent(markdown.data.content)
  }, [markdown.data])

  const dirty = !!markdown.data && editorContent !== markdown.data.content
  const filteredSkills = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    return (skills.data || []).filter(skill => {
      if (filter === 'published' && !skill.current_version) return false
      if (filter === 'draft' && skill.current_version) return false
      if (!keyword) return true
      return [skill.logical_name, skill.name, skill.description]
        .some(value => value?.toLowerCase().includes(keyword))
    })
  }, [filter, search, skills.data])
  const publishedCount = (skills.data || []).filter(skill => !!skill.current_version).length
  const previewHtml = useMemo(
    () => DOMPurify.sanitize(marked.parse(editorContent) as string),
    [editorContent],
  )

  const refresh = (skillId = selectedSkillId) => {
    queryClient.invalidateQueries({ queryKey: ['skills'] })
    if (skillId) queryClient.invalidateQueries({ queryKey: ['skill-versions', skillId] })
  }

  const confirmDiscard = (action: () => void) => {
    if (!dirty) {
      action()
      return
    }
    Modal.confirm({
      title: '放弃尚未保存的修改？',
      content: '切换 Skill、版本或文件后，当前编辑内容将丢失。',
      okText: '放弃并切换',
      cancelText: '继续编辑',
      onOk: action,
    })
  }

  const create = useMutation({
    mutationFn: ({ logical_name, file }: { logical_name: string; file: File }) => {
      const data = new FormData()
      data.append('logical_name', logical_name)
      data.append('package', file)
      return client.post('/skills', data, { headers: { ...headers(), 'Content-Type': 'multipart/form-data' } })
    },
    onSuccess: response => {
      message.success('Skill 包已上传')
      setUploadOpen(false)
      setPackageFile(undefined)
      form.resetFields()
      setSelectedSkillId(response.data.id)
      refresh(response.data.id)
    },
    onError: (error: any) => message.error(apiError(error, '上传失败')),
  })
  const uploadVersion = useMutation({
    mutationFn: (file: File) => {
      const data = new FormData()
      data.append('package', file)
      return client.post(`/skills/${selectedSkillId}/versions`, data, {
        headers: { ...headers(), 'Content-Type': 'multipart/form-data' },
      })
    },
    onSuccess: response => {
      message.success(`v${response.data.version_no} 已上传，等待发布`)
      queryClient.setQueryData<SkillVersion[]>(
        ['skill-versions', selectedSkillId],
        current => current?.some(version => version.id === response.data.id)
          ? current
          : [...(current || []), response.data],
      )
      setSelectedVersionId(response.data.id)
      setSelectedPath('SKILL.md')
      refresh()
    },
    onError: (error: any) => message.error(apiError(error, '新版本上传失败')),
  })
  const publish = useMutation({
    mutationFn: (version: SkillVersion) => client.post(
      `/skills/${selectedSkillId}/versions/${version.id}/publish`, {}, { headers: headers() },
    ),
    onSuccess: response => {
      message.success(`v${response.data.version_no} 已发布，新 Runner 将使用该 checksum`)
      setSelectedVersionId(response.data.id)
      refresh()
    },
    onError: (error: any) => message.error(apiError(error, '发布失败')),
  })
  const sync = useMutation({
    mutationFn: () => client.post(`/skills/${selectedSkillId}/sync`, {}, { headers: headers() }),
    onSuccess: () => message.success('共享只读 Skill Workspace 已同步'),
    onError: (error: any) => message.error(apiError(error, '同步失败')),
  })
  const saveRevision = useMutation({
    mutationFn: ({ versionId, path, content }: { versionId: string; path: string; content: string }) => client.post(
      `/skills/${selectedSkillId}/versions/${versionId}/markdown`,
      { path, content },
      { headers: headers() },
    ),
    onSuccess: (response, variables) => {
      message.success(`修改已保存为 v${response.data.version_no}，原版本保持不变`)
      queryClient.setQueryData<SkillVersion[]>(
        ['skill-versions', selectedSkillId],
        current => current?.some(version => version.id === response.data.id)
          ? current
          : [...(current || []), response.data],
      )
      queryClient.invalidateQueries({ queryKey: ['skills'] })
      queryClient.invalidateQueries({ queryKey: ['skill-versions', selectedSkillId] })
      setSelectedVersionId(response.data.id)
      setSelectedPath(variables.path)
      setEditorContent(variables.content)
    },
    onError: (error: any) => message.error(apiError(error, '保存新版本失败')),
  })

  const contentPanel = (
    <div className="space-y-4">
      <Alert
        showIcon
        type="info"
        message="不可变版本编辑"
        description="页面修改不会覆盖当前包；保存后会生成一个新的待发布版本，确认无误后再发布。"
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Space wrap>
          <Typography.Text type="secondary">查看版本</Typography.Text>
          <Select
            value={selectedVersionId || undefined}
            style={{ width: 190 }}
            loading={versions.isLoading}
            options={sortedVersions.map(version => ({
              value: version.id,
              label: `v${version.version_no} · ${version.status === 'published' ? '已发布' : version.status === 'uploaded' ? '待发布' : version.status}`,
            }))}
            onChange={value => confirmDiscard(() => {
              setSelectedVersionId(value)
              setSelectedPath('SKILL.md')
            })}
          />
          {selectedVersion && statusTag(selectedVersion.status)}
          {selectedVersion && (
            <Typography.Text code copyable={{ text: selectedVersion.checksum }}>
              {selectedVersion.checksum.slice(0, 12)}
            </Typography.Text>
          )}
        </Space>
        <Space wrap>
          <Upload
            accept=".skill,.zip,.tar.gz,.tgz"
            showUploadList={false}
            beforeUpload={file => { uploadVersion.mutate(file); return false }}
          >
            <Button icon={<UploadOutlined />} loading={uploadVersion.isPending}>上传新版本</Button>
          </Upload>
          {selectedVersion && !['published', 'deprecated'].includes(selectedVersion.status) && (
            <Button
              type="primary"
              icon={<CheckCircleOutlined />}
              loading={publish.isPending}
              onClick={() => publish.mutate(selectedVersion)}
            >
              发布 v{selectedVersion.version_no}
            </Button>
          )}
        </Space>
      </div>

      <div className="grid min-h-[590px] overflow-hidden rounded-xl border bg-card lg:grid-cols-[230px_minmax(0,1fr)]">
        <aside className="border-b bg-muted/30 p-3 lg:border-b-0 lg:border-r">
          <div className="mb-3 flex items-center justify-between px-1">
            <Typography.Text strong>Markdown 文件</Typography.Text>
            <Tag>{markdown.data?.files.length || 0}</Tag>
          </div>
          {markdown.isLoading ? (
            <div className="flex min-h-32 items-center justify-center"><Spin size="small" /></div>
          ) : markdown.isError ? (
            <Alert type="error" showIcon message={apiError(markdown.error, '文件读取失败')} />
          ) : (
            <div className="space-y-1">
              {(markdown.data?.files || []).map(file => (
                <button
                  type="button"
                  key={file.path}
                  onClick={() => confirmDiscard(() => setSelectedPath(file.path))}
                  className={`flex w-full items-start gap-2 rounded-lg px-3 py-2.5 text-left transition-colors ${
                    selectedPath === file.path
                      ? 'bg-primary text-primary-foreground'
                      : 'hover:bg-accent text-foreground'
                  }`}
                >
                  <FileMarkdownOutlined className="mt-0.5 shrink-0" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{file.path.split('/').pop()}</span>
                    <span className={`block truncate text-xs ${selectedPath === file.path ? 'opacity-70' : 'text-muted-foreground'}`}>
                      {file.path.includes('/') ? file.path.split('/').slice(0, -1).join('/') : '根目录'} · {formatBytes(file.size)}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </aside>

        <section className="min-w-0 p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <Typography.Text strong className="block truncate">{selectedPath}</Typography.Text>
              <Typography.Text type="secondary" className="text-xs">
                {dirty ? '有尚未保存的修改' : '内容已与所选版本一致'}
              </Typography.Text>
            </div>
            <Space>
              <Segmented
                value={contentMode}
                options={[{ label: '编辑', value: 'edit' }, { label: '预览', value: 'preview' }]}
                onChange={value => setContentMode(value as 'edit' | 'preview')}
              />
              <Tooltip title={!dirty ? '修改内容后可保存为新版本' : undefined}>
                <Button
                  type="primary"
                  icon={<SaveOutlined />}
                  disabled={!dirty || !selectedVersion}
                  loading={saveRevision.isPending}
                  onClick={() => selectedVersion && saveRevision.mutate({
                    versionId: selectedVersion.id,
                    path: selectedPath,
                    content: editorContent,
                  })}
                >
                  保存为新版本
                </Button>
              </Tooltip>
            </Space>
          </div>
          {markdown.isLoading ? (
            <div className="flex h-[520px] items-center justify-center"><Spin /></div>
          ) : markdown.isError ? (
            <Empty description="无法读取这个版本的 Markdown 内容" />
          ) : contentMode === 'edit' ? (
            <PromptEditor value={editorContent} onChange={setEditorContent} height={520} language="markdown" />
          ) : (
            <div className="message-markdown h-[520px] overflow-auto rounded-lg border bg-background p-5" dangerouslySetInnerHTML={{ __html: previewHtml }} />
          )}
        </section>
      </div>
    </div>
  )

  const versionPanel = (
    <Table
      rowKey="id"
      loading={versions.isLoading}
      dataSource={sortedVersions}
      pagination={false}
      scroll={{ x: 900 }}
      columns={[
        { title: '版本', dataIndex: 'version_no', width: 80, render: value => <Typography.Text strong>v{value}</Typography.Text> },
        { title: '状态', dataIndex: 'status', width: 100, render: statusTag },
        { title: '包', width: 120, render: (_, version: SkillVersion) => `${version.archive_type || '—'} · ${formatBytes(version.package_size)}` },
        { title: 'Checksum', dataIndex: 'checksum', width: 180, render: value => <Typography.Text code copyable={{ text: value }}>{value.slice(0, 16)}</Typography.Text> },
        { title: '内容哈希', dataIndex: 'content_hash', width: 150, render: value => <Typography.Text code>{value?.slice(0, 12) || '—'}</Typography.Text> },
        { title: '创建时间', dataIndex: 'created_at', width: 170, render: value => value ? value.replace('T', ' ').slice(0, 19) : '—' },
        {
          title: '操作', width: 150, fixed: 'right',
          render: (_, version: SkillVersion) => (
            <Space>
              <Button size="small" onClick={() => confirmDiscard(() => {
                setSelectedVersionId(version.id)
                setSelectedPath('SKILL.md')
              })}>查看内容</Button>
              {!['published', 'deprecated'].includes(version.status) && (
                <Button size="small" type="primary" loading={publish.isPending} onClick={() => publish.mutate(version)}>发布</Button>
              )}
            </Space>
          ),
        },
      ]}
    />
  )

  return (
    <>
      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <Card size="small" className="rounded-xl shadow-sm">
          <Typography.Text type="secondary">Skill 总数</Typography.Text>
          <div className="mt-1 text-2xl font-semibold">{skills.data?.length || 0}</div>
        </Card>
        <Card size="small" className="rounded-xl shadow-sm">
          <Typography.Text type="secondary">已发布</Typography.Text>
          <div className="mt-1 flex items-center gap-2 text-2xl font-semibold"><CheckCircleOutlined className="text-emerald-500" />{publishedCount}</div>
        </Card>
        <Card size="small" className="rounded-xl shadow-sm">
          <Typography.Text type="secondary">未建立发布版本的 Skill</Typography.Text>
          <div className="mt-1 flex items-center gap-2 text-2xl font-semibold"><ClockCircleOutlined className="text-amber-500" />{Math.max((skills.data?.length || 0) - publishedCount, 0)}</div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <Card className="rounded-xl shadow-sm xl:sticky xl:top-0 xl:self-start" styles={{ body: { padding: 16 } }}>
          <div className="mb-4 flex items-center justify-between gap-2">
            <div>
              <Typography.Title level={5} style={{ margin: 0 }}>Skill 库</Typography.Title>
              <Typography.Text type="secondary" className="text-xs">选择一个 Skill 管理内容与版本</Typography.Text>
            </div>
            <Button type="primary" shape="circle" icon={<PlusOutlined />} title="上传 Skill 包" onClick={() => setUploadOpen(true)} />
          </div>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索名称或描述"
            value={search}
            onChange={event => setSearch(event.target.value)}
          />
          <Segmented
            block
            className="my-3"
            value={filter}
            options={[{ label: '全部', value: 'all' }, { label: '已发布', value: 'published' }, { label: '待发布', value: 'draft' }]}
            onChange={value => setFilter(value as 'all' | 'published' | 'draft')}
          />
          {skills.isLoading ? (
            <div className="flex min-h-64 items-center justify-center"><Spin /></div>
          ) : filteredSkills.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的 Skill" />
          ) : (
            <div className="max-h-[760px] space-y-2 overflow-auto pr-1">
              {filteredSkills.map(skill => {
                const active = skill.id === selectedSkillId
                return (
                  <button
                    type="button"
                    key={skill.id}
                    onClick={() => confirmDiscard(() => setSelectedSkillId(skill.id))}
                    className={`w-full rounded-xl border p-3 text-left transition-all ${
                      active
                        ? 'border-primary bg-accent shadow-sm'
                        : 'border-border bg-card hover:border-muted-foreground/40 hover:bg-muted/40'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className="truncate font-medium">{skill.name}</span>
                      {skill.current_version ? <Tag color="success">v{skill.current_version.version_no}</Tag> : <Tag>未发布</Tag>}
                    </div>
                    <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">{skill.logical_name}</div>
                    <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted-foreground">{skill.description}</p>
                  </button>
                )
              })}
            </div>
          )}
        </Card>

        <Card className="min-w-0 rounded-xl shadow-sm" styles={{ body: { padding: 20 } }}>
          {!selectedSkill ? (
            <Empty description="选择一个 Skill 查看详情" />
          ) : (
            <>
              <div className="mb-2 flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Typography.Title level={4} style={{ margin: 0 }}>{selectedSkill.name}</Typography.Title>
                    <Tag color={selectedSkill.status === 'active' ? 'success' : 'default'}>{selectedSkill.status}</Tag>
                    {selectedSkill.current_version ? <Tag color="blue">当前 v{selectedSkill.current_version.version_no}</Tag> : <Tag>尚未发布</Tag>}
                  </div>
                  <Typography.Paragraph type="secondary" className="!mb-1 !mt-2 max-w-4xl">
                    {selectedSkill.description}
                  </Typography.Paragraph>
                  <Typography.Text type="secondary" className="font-mono text-xs">{selectedSkill.logical_name}</Typography.Text>
                </div>
                <Space wrap>
                  <Button
                    icon={<SyncOutlined />}
                    disabled={!selectedSkill.current_version}
                    loading={sync.isPending}
                    onClick={() => sync.mutate()}
                  >
                    同步 Workspace
                  </Button>
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => setUploadOpen(true)}>上传 Skill</Button>
                </Space>
              </div>
              <Tabs
                defaultActiveKey="content"
                items={[
                  { key: 'content', label: '内容与编辑', children: contentPanel },
                  { key: 'versions', label: `版本历史 (${sortedVersions.length})`, children: versionPanel },
                ]}
              />
            </>
          )}
        </Card>
      </div>

      <Modal
        title="上传通用 Skill 包"
        open={uploadOpen}
        onCancel={() => setUploadOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={create.isPending}
        okText="上传"
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={values => packageFile && create.mutate({ logical_name: values.logical_name, file: packageFile })}
        >
          <Form.Item
            name="logical_name"
            label="逻辑标识"
            extra="用于平台内稳定引用；包内展示名称仍读取 SKILL.md frontmatter。"
            rules={[{ required: true, message: '请输入逻辑标识' }]}
          >
            <Input placeholder="professional_writer" />
          </Form.Item>
          <Form.Item label="包文件" required>
            <Upload.Dragger
              accept=".skill,.zip,.tar.gz,.tgz"
              maxCount={1}
              beforeUpload={file => { setPackageFile(file); return false }}
              onRemove={() => setPackageFile(undefined)}
            >
              <p><InboxOutlined style={{ fontSize: 30 }} /></p>
              <p>拖入包含根目录 SKILL.md 的 .skill、zip 或 tar.gz</p>
            </Upload.Dragger>
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
