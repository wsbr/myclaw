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
  type KnowledgeLexiconTerm, type KnowledgeSearchResult,
  type KnowledgeStorageReconciliation,
} from '@/api/knowledge'
import { displayLabel } from '@/lib/presentation'


type CreateValues = {
  name: string
  description?: string
  agent_ids: string[]
  embedding_model_id?: string
}


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
  const [embeddingModelId, setEmbeddingModelId] = useState<string>()
  const [file, setFile] = useState<File | null>(null)
  const [searchText, setSearchText] = useState('')
  const [searchResults, setSearchResults] = useState<KnowledgeSearchResult[]>([])
  const [degradedRoutes, setDegradedRoutes] = useState<unknown[]>([])
  const [lexiconOpen, setLexiconOpen] = useState(false)
  const [lexiconDraft, setLexiconDraft] = useState<KnowledgeLexiconTerm[]>([])
  const [reconciliation, setReconciliation] = useState<KnowledgeStorageReconciliation | null>(null)

  const bases = useQuery({
    queryKey: ['knowledge-bases'], queryFn: knowledgeApi.list,
    refetchInterval: query => query.state.data?.some(item => item.pending_index_revision) ? 1500 : false,
  })
  const agents = useQuery({ queryKey: ['agents'], queryFn: agentsApi.list })
  const embeddingModels = useQuery({
    queryKey: ['knowledge-embedding-models'], queryFn: knowledgeApi.listEmbeddingModels,
  })
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
    setEmbeddingModelId(selected?.embedding_model_id ?? undefined)
    setSearchResults([])
    setSearchText('')
    setDegradedRoutes([])
    setFile(null)
    setReconciliation(null)
  }, [selected?.id, selected?.agent_ids, selected?.embedding_model_id])

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
      embedding_model_id: values.embedding_model_id,
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
    onSuccess: async () => { message.success('智能体访问范围已更新'); await refresh() },
    onError: () => message.error('访问范围更新失败'),
  })

  const updateEmbedding = useMutation({
    mutationFn: () => knowledgeApi.updateEmbeddingModel(selected!.id, embeddingModelId!),
    onSuccess: async () => {
      message.success('语义模型已更新，后台正在重建向量索引')
      await refresh()
    },
    onError: () => message.error('语义模型更新失败'),
  })

  const upload = useMutation({
    mutationFn: () => knowledgeApi.upload(selected!.id, file!),
    onSuccess: async (result) => {
      setFile(null)
      if (result.source.status === 'failed') {
        message.warning(`文件已留痕，但解析失败：${result.source.error_code}`)
      } else if (result.idempotent_replay) {
        message.info('相同校验和已存在，未重复建立索引')
      } else {
        message.success(selected?.embedding_model_id ? '文档已接收，后台正在生成语义向量' : '文档索引已完成')
      }
      await refresh()
    },
    onError: () => message.error('上传或索引失败'),
  })

  const search = useMutation({
    mutationFn: () => knowledgeApi.search(selected!.id, searchText.trim()),
    onSuccess: result => {
      setSearchResults(result.results)
      setDegradedRoutes(result.degraded_routes)
    },
    onError: () => message.error('检索失败'),
  })

  const replaceLexicon = useMutation({
    mutationFn: () => knowledgeApi.replaceLexicon(selected!.id, lexiconDraft.map(term => ({
      ...term,
      canonical_name: term.canonical_name.trim(),
      description: term.description.trim(),
      aliases: term.aliases.map(value => value.trim()).filter(Boolean),
    }))),
    onSuccess: async result => {
      setLexiconOpen(false)
      message.success(result.idempotent_replay ? '词表内容未变化' : '词表已发布，混合索引正在重建')
      await refresh()
    },
    onError: () => message.error('领域词表发布失败，请检查重复别名或字段长度'),
  })

  const openLexicon = () => {
    setLexiconDraft((selected?.current_lexicon_revision.terms ?? []).map(term => ({
      ...term, aliases: [...term.aliases],
    })))
    setLexiconOpen(true)
  }

  const reindex = useMutation({
    mutationFn: () => knowledgeApi.reindex(selected!.id),
    onSuccess: async result => {
      message.success(result.status === 'ready' ? '索引已核对' : '后台重建任务已排队')
      await refresh()
    },
    onError: () => message.error('重建索引失败'),
  })

  const reconcileStorage = useMutation({
    mutationFn: () => knowledgeApi.reconcileStorage(selected!.id, true),
    onSuccess: async (result) => {
      setReconciliation(result)
      if (result.healthy) message.success('源文件与数据库记录一致')
      else message.warning(
        `发现 ${result.missing_sources.length} 个缺失、${result.corrupt_sources.length} 个损坏、${result.orphaned_bodies.length} 个无引用文件`,
      )
      await refresh()
    },
    onError: () => message.error('存储对账失败'),
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

  return <div className="app-page">
    <div className="flex items-start justify-between gap-4">
      <div>
        <Typography.Title level={2} style={{ margin: 0 }}>知识库</Typography.Title>
        <Typography.Text type="secondary">
          导入可引用的文档证据，通过真实嵌入与向量检索为智能体提供语义召回。
        </Typography.Text>
      </div>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建知识库</Button>
    </div>

    <Alert showIcon type="info" message="默认不向任何智能体开放"
      description="必须显式选择智能体，并在智能体版本中启用知识检索工具。嵌入模型使用独立模型版本；凭据仍由平台密钥服务加密管理。" />

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
                <Tag color={item.agent_ids.length ? 'blue' : 'default'}>{item.agent_ids.length} 个智能体</Tag>
                <Tag color={item.embedding_model_id ? 'purple' : 'default'}>{item.embedding_model_id ? '语义混合检索' : '兼容检索'}</Tag>
                {item.pending_index_revision && <Tag color="processing">索引中</Tag>}
              </Space>
            } />
          </List.Item>}
        /> : <Empty description="还没有知识库" />}
      </Card>

      {!selected ? <Card><Empty description="选择或创建知识库" /></Card> : <div className="space-y-4">
        <Card title={selected.name} extra={<Space>
          <Button icon={<ReloadOutlined />} loading={reindex.isPending} onClick={() => reindex.mutate()}>重建当前索引</Button>
          <Button loading={reconcileStorage.isPending} onClick={() => reconcileStorage.mutate()}>存储对账</Button>
          <Popconfirm title="停用这个知识库？" description="后续检索不可见，历史引用不会被改写。"
            onConfirm={() => removeBase.mutate()} okButtonProps={{ danger: true }}>
            <Button danger icon={<DeleteOutlined />}>停用</Button>
          </Popconfirm>
        </Space>}>
          <Typography.Paragraph type="secondary">{selected.description || '无说明'}</Typography.Paragraph>
          <Descriptions size="small" column={{ xs: 1, md: 2 }}>
            <Descriptions.Item label="索引版本">{selected.current_index_revision?.revision_no ?? '尚未索引'}</Descriptions.Item>
            <Descriptions.Item label="片段数">{selected.current_index_revision?.chunk_count ?? 0}</Descriptions.Item>
            <Descriptions.Item label="检索配置"><Tag color={selected.current_index_revision?.vector_backend === 'qdrant' ? 'purple' : 'default'}>
              {selected.current_index_revision?.vector_backend === 'qdrant' ? '语义混合检索 · Qdrant' : '兼容混合检索'}
            </Tag></Descriptions.Item>
            <Descriptions.Item label="向量点数">{selected.current_index_revision?.vector_point_count ?? 0}</Descriptions.Item>
            <Descriptions.Item label="领域词表">
              修订 {selected.current_lexicon_revision.revision_no} · {selected.current_lexicon_revision.terms.length} 词
            </Descriptions.Item>
            <Descriptions.Item label="后台索引">
              {selected.pending_index_revision
                ? <Tag color={selected.pending_index_revision.status === 'failed' ? 'error' : 'processing'}>{displayLabel(selected.pending_index_revision.status)}</Tag>
                : '空闲'}
            </Descriptions.Item>
            <Descriptions.Item label="修订哈希">
              <Typography.Text code copyable>{selected.current_index_revision?.content_hash.slice(0, 16) ?? '—'}</Typography.Text>
            </Descriptions.Item>
          </Descriptions>
          {reconciliation && <Alert className="mt-4" showIcon
            type={reconciliation.healthy ? 'success' : 'warning'}
            message={reconciliation.healthy ? '最近一次存储对账正常' : '最近一次存储对账发现异常'}
            description={`检查 ${reconciliation.checked_source_count} 个 source；缺失 ${reconciliation.missing_sources.length}，损坏 ${reconciliation.corrupt_sources.length}，无引用文件 ${reconciliation.orphaned_bodies.length}；状态修复 ${reconciliation.changed_source_ids.length}。无引用文件仅报告，不自动删除。`} />}
        </Card>

        <Card title="语义检索模型">
          <Space.Compact className="w-full">
            <Select className="w-full" value={embeddingModelId} onChange={setEmbeddingModelId}
              placeholder="选择已通过兼容性验证并激活的嵌入模型"
              options={(embeddingModels.data ?? []).map(model => ({
                value: model.id,
                label: `${model.name} · ${model.model_name} · ${model.dimensions}d`,
              }))} />
            <Button type="primary" disabled={!embeddingModelId || embeddingModelId === selected.embedding_model_id}
              loading={updateEmbedding.isPending} onClick={() => updateEmbedding.mutate()}>应用并重建</Button>
          </Space.Compact>
          {!embeddingModels.isLoading && !embeddingModels.data?.length && <Alert className="mt-3" showIcon type="warning"
            message="尚无可用嵌入模型"
            description="请先在管理后台创建独立逻辑模型，追加嵌入模型版本，运行兼容性验证并激活。" />}
        </Card>

        <Card title="智能体访问范围">
          <Space.Compact className="w-full">
            <Select mode="multiple" className="w-full" value={agentIds} onChange={setAgentIds}
              placeholder="选择允许检索此知识库的智能体"
              options={(agents.data ?? []).map(agent => ({ value: agent.id, label: agent.name }))} />
            <Button type="primary" loading={updateAccess.isPending} onClick={() => updateAccess.mutate()}>保存</Button>
          </Space.Compact>
        </Card>

        <Card title="领域词表" extra={<Button onClick={openLexicon}>编辑并发布修订</Button>}>
          <Typography.Paragraph type="secondary">
            为平台术语、产品名、错误码和业务别名建立第三路召回。每次发布生成不可变修订，并触发向量 + BM25 + 词表的全量重建。
          </Typography.Paragraph>
          {selected.current_lexicon_revision.terms.length ? <Space wrap>
            {selected.current_lexicon_revision.terms.map(term => <Tag key={term.id ?? term.canonical_name} color="cyan">
              {term.canonical_name}{term.aliases.length ? `（${term.aliases.join(' / ')}）` : ''}
            </Tag>)}
          </Space> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未配置领域词表；检索仍使用向量 + BM25" />}
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
              { title: '校验和', key: 'checksum', render: (_: unknown, item: KnowledgeDocument) => <Typography.Text code copyable>{item.current_source?.checksum.slice(0, 12) ?? '—'}</Typography.Text> },
              { title: '状态', key: 'status', render: (_: unknown, item: KnowledgeDocument) => <Tag color={item.current_source?.status === 'indexed' ? 'success' : 'error'}>{displayLabel(item.current_source?.status ?? item.status)}</Tag> },
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
          {degradedRoutes.length > 0 && <Alert className="mb-3" type="warning" showIcon
            message="部分检索路由已降级" description={JSON.stringify(degradedRoutes)} />}
          {searchResults.length ? <List dataSource={searchResults} renderItem={item => <List.Item>
            <List.Item.Meta title={<Space wrap>
              <Typography.Text strong>{citationLabel(item)}</Typography.Text>
              <Tag color="blue">相关度 {item.score.toFixed(3)}</Tag>
              <Tag color={item.retrieval_kind === 'hybrid_rrf' ? 'purple' : 'default'}>{item.retrieval_kind}</Tag>
              {Object.entries(item.route_evidence).map(([route, evidence]) => <Tag key={route}>
                {route} #{evidence.rank} · {evidence.score.toFixed(3)}
              </Tag>)}
              {item.matched_lexicon_terms.map(term => <Tag key={term.term_id} color="cyan">
                词表：{term.matched_value} → {term.canonical_name}
              </Tag>)}
              <Tag>修订 {item.index_revision_id.slice(0, 8)}</Tag>
            </Space>} description={<>
              <Typography.Paragraph className="mt-2 whitespace-pre-wrap">{item.excerpt}</Typography.Paragraph>
              <Typography.Text type="secondary" copyable>校验和：{item.source_checksum}</Typography.Text>
            </>} />
          </List.Item>} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="检索结果会在这里显示，并附带精确修订与校验和引用" />}
        </Card>
      </div>}
    </div>

    <Modal title="新建知识库" open={createOpen} onCancel={() => setCreateOpen(false)}
      onOk={() => createForm.submit()} confirmLoading={create.isPending}>
      <Form form={createForm} layout="vertical" onFinish={values => create.mutate(values)} initialValues={{ agent_ids: [] }}>
        <Form.Item name="name" label="名称" rules={[{ required: true }, { max: 200 }]}><Input /></Form.Item>
        <Form.Item name="description" label="说明"><Input.TextArea rows={3} maxLength={5000} /></Form.Item>
        <Form.Item name="agent_ids" label="允许检索的智能体">
          <Select mode="multiple" options={(agents.data ?? []).map(agent => ({ value: agent.id, label: agent.name }))} />
        </Form.Item>
        <Form.Item name="embedding_model_id" label="嵌入模型" extra="可稍后配置；未配置时使用兼容旧索引。">
          <Select allowClear options={(embeddingModels.data ?? []).map(model => ({
            value: model.id, label: `${model.name} · ${model.model_name} · ${model.dimensions}d`,
          }))} />
        </Form.Item>
      </Form>
    </Modal>
    <Modal title="发布领域词表修订" width={960} open={lexiconOpen}
      onCancel={() => setLexiconOpen(false)} onOk={() => replaceLexicon.mutate()}
      okText="发布并重建" confirmLoading={replaceLexicon.isPending}
      okButtonProps={{ disabled: lexiconDraft.some(term => !term.canonical_name.trim()) }}>
      <Alert className="mb-4" showIcon type="info" message="别名在同一词表中必须唯一"
        description="文档命中规范词或任一别名时，会写入同一个稳定术语 ID；用户查询命中别名后可召回包含规范词的片段。" />
      <Table rowKey={(_, index) => String(index)} pagination={false} size="small"
        dataSource={lexiconDraft} columns={[
          { title: '规范词', width: 180, render: (_: unknown, item: KnowledgeLexiconTerm, index: number) => <Input
            value={item.canonical_name} maxLength={200} onChange={event => setLexiconDraft(current => current.map((term, i) => i === index ? { ...term, canonical_name: event.target.value } : term))} /> },
          { title: '类型', width: 120, render: (_: unknown, item: KnowledgeLexiconTerm, index: number) => <Select
            className="w-full" value={item.term_type} onChange={value => setLexiconDraft(current => current.map((term, i) => i === index ? { ...term, term_type: value } : term))}
            options={['concept', 'entity', 'code', 'product', 'agent', 'other'].map(value => ({ value, label: displayLabel(value) }))} /> },
          { title: '别名（逗号分隔）', render: (_: unknown, item: KnowledgeLexiconTerm, index: number) => <Input
            value={item.aliases.join(', ')} onChange={event => setLexiconDraft(current => current.map((term, i) => i === index ? { ...term, aliases: event.target.value.split(/[,，]/) } : term))} /> },
          { title: '说明', render: (_: unknown, item: KnowledgeLexiconTerm, index: number) => <Input
            value={item.description} maxLength={2000} onChange={event => setLexiconDraft(current => current.map((term, i) => i === index ? { ...term, description: event.target.value } : term))} /> },
          { title: '', width: 64, render: (_: unknown, _item: KnowledgeLexiconTerm, index: number) => <Button danger type="link" onClick={() => setLexiconDraft(current => current.filter((_, i) => i !== index))}>删除</Button> },
        ]} />
      <Button className="mt-3" icon={<PlusOutlined />} onClick={() => setLexiconDraft(current => [...current, {
        canonical_name: '', term_type: 'concept', description: '', aliases: [],
      }])}>添加术语</Button>
    </Modal>
  </div>
}
