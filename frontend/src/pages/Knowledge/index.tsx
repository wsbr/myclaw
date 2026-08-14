import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Button, Card, Descriptions, Empty, Form, Input, List, Modal,
  Popconfirm, Select, Space, Spin, Table, Tag, Typography, Upload, message,
} from 'antd'
import {
  DeleteOutlined, FileSearchOutlined, InboxOutlined, PlusOutlined,
  ReloadOutlined, UploadOutlined,
} from '@ant-design/icons'

import { agentsApi } from '@/api/agents'
import {
  knowledgeApi, type KnowledgeBase, type KnowledgeDocument,
  type KnowledgeSearchResult,
} from '@/api/knowledge'


type CreateValues = { name: string; description?: string; agent_ids: string[] }


function citationLabel(item: KnowledgeSearchResult) {
  const location = item.page_number
    ? `第 ${item.page_number} 页`
    : item.section ? `章节：${item.section}` : '文档片段'
  return `${item.filename} · ${location}`
}


export default function KnowledgePage() {
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createForm] = Form.useForm<CreateValues>()
  const [agentIds, setAgentIds] = useState<string[]>([])
  const [file, setFile] = useState<File | null>(null)
  const [searchText, setSearchText] = useState('')
  const [searchResults, setSearchResults] = useState<KnowledgeSearchResult[]>([])

  const bases = useQuery({ queryKey: ['knowledge-bases'], queryFn: knowledgeApi.list })
  const agents = useQuery({ queryKey: ['agents'], queryFn: agentsApi.list })
  const selected = useMemo(
    () => bases.data?.find(item => item.id === selectedId) ?? null,
    [bases.data, selectedId],
  )
  const documents = useQuery({
    queryKey: ['knowledge-documents', selectedId],
    queryFn: () => knowledgeApi.listDocuments(selectedId!),
    enabled: !!selectedId,
  })

  useEffect(() => {
    if (!selectedId && bases.data?.length) setSelectedId(bases.data[0].id)
    if (selectedId && bases.data && !bases.data.some(item => item.id === selectedId)) {
      setSelectedId(bases.data[0]?.id ?? null)
    }
  }, [bases.data, selectedId])

  useEffect(() => {
    setAgentIds(selected?.agent_ids ?? [])
    setSearchResults([])
    setSearchText('')
    setFile(null)
  }, [selected?.id, selected?.agent_ids])

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] }),
      queryClient.invalidateQueries({ queryKey: ['knowledge-documents', selectedId] }),
    ])
  }

  const create = useMutation({
    mutationFn: (values: CreateValues) => knowledgeApi.create({
      name: values.name.trim(), description: values.description?.trim() ?? '',
      agent_ids: values.agent_ids ?? [],
    }),
    onSuccess: async (item) => {
      message.success('知识库已创建')
      setSelectedId(item.id)
      setCreateOpen(false)
      createForm.resetFields()
      await refresh()
    },
    onError: () => message.error('知识库创建失败'),
  })

  const updateAccess = useMutation({
    mutationFn: () => knowledgeApi.updateAccess(selected!.id, agentIds),
    onSuccess: async () => { message.success('Agent 访问范围已更新'); await refresh() },
    onError: () => message.error('访问范围更新失败'),
  })

  const upload = useMutation({
    mutationFn: () => knowledgeApi.upload(selected!.id, file!),
    onSuccess: async (result) => {
      setFile(null)
      if (result.source.status === 'failed') {
        message.warning(`文件已留痕，但解析失败：${result.source.error_code}`)
      } else if (result.idempotent_replay) {
        message.info('相同 checksum 已存在，未重复建立索引')
      } else {
        message.success('文档已在本机完成索引')
      }
      await refresh()
    },
    onError: () => message.error('上传或索引失败'),
  })

  const search = useMutation({
    mutationFn: () => knowledgeApi.search(selected!.id, searchText.trim()),
    onSuccess: result => setSearchResults(result.results),
    onError: () => message.error('检索失败'),
  })

  const reindex = useMutation({
    mutationFn: () => knowledgeApi.reindex(selected!.id),
    onSuccess: async () => { message.success('索引已核对'); await refresh() },
    onError: () => message.error('重建索引失败'),
  })

  const removeDocument = useMutation({
    mutationFn: (document: KnowledgeDocument) => knowledgeApi.deleteDocument(selected!.id, document.id),
    onSuccess: async () => {
      message.success('文档已停止参与后续检索，历史引用仍保留')
      setSearchResults([])
      await refresh()
    },
    onError: () => message.error('删除失败'),
  })

  const removeBase = useMutation({
    mutationFn: () => knowledgeApi.deleteBase(selected!.id),
    onSuccess: async () => {
      message.success('知识库已停用')
      setSelectedId(null)
      await refresh()
    },
    onError: () => message.error('知识库停用失败'),
  })

  if (bases.isLoading) return <div className="py-24 text-center"><Spin /></div>

  return <div className="space-y-4">
    <div className="flex items-start justify-between gap-4">
      <div>
        <Typography.Title level={2} style={{ margin: 0 }}>本地知识库</Typography.Title>
        <Typography.Text type="secondary">
          导入可引用的文档证据；Knowledge 与记录用户事实的 Memory 相互独立。
        </Typography.Text>
      </div>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建知识库</Button>
    </div>

    <Alert showIcon type="info" message="默认不向任何 Agent 开放"
      description="源文件、特征向量和索引都保存在当前 MyClaw 节点；必须显式选择 Agent 并在 AgentVersion 中启用 knowledge_search Tool，模型才能检索。" />

    <div className="grid gap-4 xl:grid-cols-[300px_minmax(0,1fr)]">
      <Card title="知识库" styles={{ body: { padding: 8 } }}>
        {bases.data?.length ? <List
          dataSource={bases.data}
          renderItem={(item: KnowledgeBase) => <List.Item
            className={`cursor-pointer rounded px-3 transition-colors ${item.id === selectedId ? 'bg-blue-50' : 'hover:bg-gray-50'}`}
            onClick={() => setSelectedId(item.id)}>
            <List.Item.Meta title={item.name} description={
              <Space size={4} wrap>
                <Tag>{item.document_count} 文档</Tag>
                <Tag color={item.agent_ids.length ? 'blue' : 'default'}>{item.agent_ids.length} Agent</Tag>
              </Space>
            } />
          </List.Item>}
        /> : <Empty description="还没有知识库" />}
      </Card>

      {!selected ? <Card><Empty description="选择或创建知识库" /></Card> : <div className="space-y-4">
        <Card title={selected.name} extra={<Space>
          <Button icon={<ReloadOutlined />} loading={reindex.isPending} onClick={() => reindex.mutate()}>核对索引</Button>
          <Popconfirm title="停用这个知识库？" description="后续检索不可见，历史引用不会被改写。"
            onConfirm={() => removeBase.mutate()} okButtonProps={{ danger: true }}>
            <Button danger icon={<DeleteOutlined />}>停用</Button>
          </Popconfirm>
        </Space>}>
          <Typography.Paragraph type="secondary">{selected.description || '无说明'}</Typography.Paragraph>
          <Descriptions size="small" column={{ xs: 1, md: 2 }}>
            <Descriptions.Item label="索引版本">{selected.current_index_revision?.revision_no ?? '尚未索引'}</Descriptions.Item>
            <Descriptions.Item label="片段数">{selected.current_index_revision?.chunk_count ?? 0}</Descriptions.Item>
            <Descriptions.Item label="检索配置"><Tag color="green">纯本地 Hybrid</Tag></Descriptions.Item>
            <Descriptions.Item label="Revision hash">
              <Typography.Text code copyable>{selected.current_index_revision?.content_hash.slice(0, 16) ?? '—'}</Typography.Text>
            </Descriptions.Item>
          </Descriptions>
        </Card>

        <Card title="Agent 访问范围">
          <Space.Compact className="w-full">
            <Select mode="multiple" className="w-full" value={agentIds} onChange={setAgentIds}
              placeholder="选择允许检索此知识库的 Agent"
              options={(agents.data ?? []).map(agent => ({ value: agent.id, label: agent.name }))} />
            <Button type="primary" loading={updateAccess.isPending} onClick={() => updateAccess.mutate()}>保存</Button>
          </Space.Compact>
        </Card>

        <Card title="文档">
          <Space className="mb-4" wrap>
            <Upload accept=".txt,.md,.markdown,.pdf" maxCount={1} fileList={file ? [{
              uid: file.name, name: file.name, status: 'done' as const,
            }] : []} beforeUpload={(next) => { setFile(next); return false }}
              onRemove={() => { setFile(null); return true }}>
              <Button icon={<InboxOutlined />}>选择 TXT / Markdown / PDF</Button>
            </Upload>
            <Button type="primary" icon={<UploadOutlined />} disabled={!file}
              loading={upload.isPending} onClick={() => upload.mutate()}>上传并索引</Button>
          </Space>
          <Table rowKey="id" size="small" loading={documents.isLoading}
            dataSource={documents.data ?? []} pagination={false} columns={[
              { title: '文件', dataIndex: 'title' },
              { title: '类型', key: 'media', render: (_: unknown, item: KnowledgeDocument) => item.current_source?.media_type ?? '—' },
              { title: '大小', key: 'size', render: (_: unknown, item: KnowledgeDocument) => item.current_source ? `${Math.ceil(item.current_source.size_bytes / 1024)} KiB` : '—' },
              { title: 'Checksum', key: 'checksum', render: (_: unknown, item: KnowledgeDocument) => <Typography.Text code copyable>{item.current_source?.checksum.slice(0, 12) ?? '—'}</Typography.Text> },
              { title: '状态', key: 'status', render: (_: unknown, item: KnowledgeDocument) => <Tag color={item.current_source?.status === 'indexed' ? 'success' : 'error'}>{item.current_source?.status ?? item.status}</Tag> },
              { title: '操作', key: 'actions', render: (_: unknown, item: KnowledgeDocument) => <Popconfirm title="停止后续检索？" onConfirm={() => removeDocument.mutate(item)}><Button danger type="link" size="small">删除</Button></Popconfirm> },
            ]} />
        </Card>

        <Card title="检索与引用">
          <Space.Compact className="mb-4 w-full">
            <Input value={searchText} onChange={event => setSearchText(event.target.value)}
              onPressEnter={() => searchText.trim() && search.mutate()}
              placeholder="输入要从文档中查证的问题" />
            <Button type="primary" icon={<FileSearchOutlined />} disabled={!searchText.trim()}
              loading={search.isPending} onClick={() => search.mutate()}>检索</Button>
          </Space.Compact>
          {searchResults.length ? <List dataSource={searchResults} renderItem={item => <List.Item>
            <List.Item.Meta title={<Space wrap>
              <Typography.Text strong>{citationLabel(item)}</Typography.Text>
              <Tag color="blue">score {item.score.toFixed(3)}</Tag>
              <Tag>revision {item.index_revision_id.slice(0, 8)}</Tag>
            </Space>} description={<>
              <Typography.Paragraph className="mt-2 whitespace-pre-wrap">{item.excerpt}</Typography.Paragraph>
              <Typography.Text type="secondary" copyable>checksum: {item.source_checksum}</Typography.Text>
            </>} />
          </List.Item>} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="检索结果会在这里显示，并附带精确 revision/checksum 引用" />}
        </Card>
      </div>}
    </div>

    <Modal title="新建本地知识库" open={createOpen} onCancel={() => setCreateOpen(false)}
      onOk={() => createForm.submit()} confirmLoading={create.isPending}>
      <Form form={createForm} layout="vertical" onFinish={values => create.mutate(values)} initialValues={{ agent_ids: [] }}>
        <Form.Item name="name" label="名称" rules={[{ required: true }, { max: 200 }]}><Input /></Form.Item>
        <Form.Item name="description" label="说明"><Input.TextArea rows={3} maxLength={5000} /></Form.Item>
        <Form.Item name="agent_ids" label="允许检索的 Agent">
          <Select mode="multiple" options={(agents.data ?? []).map(agent => ({ value: agent.id, label: agent.name }))} />
        </Form.Item>
      </Form>
    </Modal>
  </div>
}
