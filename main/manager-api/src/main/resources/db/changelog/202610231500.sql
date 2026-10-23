-- 联网搜索插件新增阿里云AI搜索源及其连接配置

UPDATE `ai_model_provider`
SET `fields` = JSON_ARRAY_APPEND(
    JSON_SET(
        `fields`,
        '$[0].label',
        '搜索源：metaso / tavily / serply / aliyun'
    ),
    '$',
    JSON_OBJECT(
        'key', 'host',
        'type', 'string',
        'label', '阿里云服务接入地址（仅aliyun）',
        'default', '',
        'editing', FALSE,
        'selected', FALSE
    ),
    JSON_OBJECT(
        'key', 'workspace_name',
        'type', 'string',
        'label', '阿里云工作空间（仅aliyun）',
        'default', 'default',
        'editing', FALSE,
        'selected', FALSE
    ),
    JSON_OBJECT(
        'key', 'service_id',
        'type', 'string',
        'label', '阿里云服务ID（仅aliyun）',
        'default', 'ops-web-search-001',
        'editing', FALSE,
        'selected', FALSE
    )
)
WHERE `id` = 'SYSTEM_PLUGIN_WEB_SEARCH';
