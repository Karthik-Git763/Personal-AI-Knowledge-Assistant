-- database: :memory:
-- Enable UUID extension (for generating unique IDs uuid_generate_v4)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable trigram extension (for fuzzy search)
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Users Table
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    avatar_url TEXT,
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    is_verified BOOLEAN DEFAULT FALSE,
    is_superuser BOOLEAN DEFAULT FALSE,
    is_deleted BOOLEAN DEFAULT FALSE,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    last_login_at TIMESTAMP,
    
    -- Constraints
    CONSTRAINT email_format CHECK (email ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')
);

-- DOCUMENT TABLE
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- File information
    title VARCHAR(255) NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    file_path TEXT NOT NULL,
    file_size BIGINT NOT NULL,
    file_type VARCHAR(255) NOT NULL,
    mime_type VARCHAR(255) NOT NULL,
    
    -- Content
    content TEXT,
    content_preview TEXT, -- First 500 chars for quick display
    
    -- AI-generated metadata
    summary TEXT,
    keywords TEXT[],
    tags TEXT[],
    language VARCHAR(10) DEFAULT 'en',
    
    -- Processing Status
    status VARCHAR(50) DEFAULT 'processing' CHECK (status IN ('processing', 'completed', 'failed', 'deleted')),
    processing_started_at TIMESTAMP,
    processing_completed_at TIMESTAMP,
    processing_error TEXT,
    
    -- Statistics
    word_count INTEGER,
    page_count INTEGER,
    chunk_count INTEGER DEFAULT 0,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    last_accessed_at TIMESTAMP DEFAULT NOW()
);

-- DOCUMENT CHUNK TABLE
CREATE TABLE IF NOT EXISTS document_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    
    -- chunk information
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash VARCHAR(255), -- FOR DEDUPLICATION
    
    -- vector information
    vector_id TEXT NOT NULL,
    
    -- metadata
    token_count INTEGER,
    char_count INTEGER,
    page_number INTEGER,
    section_title VARCHAR(255),
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    
    -- Constraints
    UNIQUE(document_id, chunk_index)
);

-- NOTE FOLDERS TABLE
CREATE TABLE IF NOT EXISTS note_folders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- FOLDER information
    name VARCHAR(255) NOT NULL,
    description TEXT,
    parent_folder_id UUID REFERENCES note_folders(id) ON DELETE CASCADE,
    
    -- Appearance
    color VARCHAR(20),
    icon VARCHAR(50),
    emoji VARCHAR(10),

    -- Settings
    is_shared BOOLEAN DEFAULT FALSE,
    is_archived BOOLEAN DEFAULT FALSE,
    sort_order INTEGER DEFAULT 0,
    is_deleted BOOLEAN DEFAULT FALSE,

    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    -- Constraints
    UNIQUE(user_id, name, parent_folder_id)
);

-- CHAT SESSIONS TABLE
CREATE TABLE IF NOT EXISTS chat_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- Session information
    title VARCHAR(255),
    description TEXT,
    
    -- Settings
    is_archived BOOLEAN DEFAULT FALSE,
    is_pinned BOOLEAN DEFAULT FALSE,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    last_message_at TIMESTAMP DEFAULT NOW()
);

-- NOTES TABLE
CREATE TABLE IF NOT EXISTS notes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder_id UUID REFERENCES note_folders(id) ON DELETE SET NULL,
    
    -- Content
    title VARCHAR(500) NOT NULL,
    content TEXT NOT NULL,
    content_type VARCHAR(20) DEFAULT 'markdown' CHECK (content_type IN ('markdown', 'html', 'plain', 'rich_text')),
    content_preview TEXT, -- First 200 chars
    
    -- AI-generated metadata
    summary TEXT,
    keywords TEXT[],
    ai_generated BOOLEAN DEFAULT FALSE,
    
    -- Organization
    is_favorite BOOLEAN DEFAULT FALSE,
    is_archived BOOLEAN DEFAULT FALSE,
    is_pinned BOOLEAN DEFAULT FALSE,
    color VARCHAR(20),
    emoji VARCHAR(10),
    
    -- Linking (bi-directional)
    linked_document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    linked_chat_session_id UUID REFERENCES chat_sessions(id) ON DELETE SET NULL,
    parent_note_id UUID REFERENCES notes(id) ON DELETE SET NULL, -- For note heirarchy
    
    -- Versioning
    version INTEGER DEFAULT 1,
    previous_version_id UUID REFERENCES notes(id) ON DELETE SET NULL,
    
    -- Collaboration
    is_public BOOLEAN DEFAULT FALSE,
    is_locked BOOLEAN DEFAULT FALSE, -- Prevent Editing
    locked_by UUID REFERENCES users(id) ON DELETE SET NULL,
    locked_at TIMESTAMP,
    
    -- Statistics
    word_count INTEGER,
    char_count INTEGER,
    read_time_minutes INTEGER, -- Estimated reading time in minutes
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    last_accessed_at TIMESTAMP DEFAULT NOW(),
    last_edited_at TIMESTAMP DEFAULT NOW()
);

-- NOTE TAGS TABLE
CREATE TABLE IF NOT EXISTS note_tags (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- Tag information
    name VARCHAR(100) NOT NULL,
    color VARCHAR(20),
    description TEXT,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    
    -- Constraints
    UNIQUE(user_id, name)
);

-- NOTE-TAG RELATIONSHIP (Many-to-Many)
CREATE TABLE IF NOT EXISTS note_tag_relations (
    note_id UUID NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag_id UUID NOT NULL REFERENCES note_tags(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT NOW(),
    
    PRIMARY KEY (note_id, tag_id)
);

-- NOTE TEMPLATES TABLE
CREATE TABLE IF NOT EXISTS note_templates (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE, -- NULL for system templates
    
    -- Template information
    name VARCHAR(255) NOT NULL,
    description TEXT,
    category VARCHAR(100), -- 'meeting', 'study', 'research', etc.
    content TEXT NOT NULL,
    content_type VARCHAR(20) DEFAULT 'markdown',
    
    -- Settings
    is_public BOOLEAN DEFAULT FALSE,
    is_system BOOLEAN DEFAULT FALSE, -- System-provided templates
    
    -- Statistics
    usage_count INTEGER DEFAULT 0,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- CHAT MESSAGES TABLE
CREATE TABLE IF NOT EXISTS chat_messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    
    -- Message content
    role VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    
    -- AI response metadata
    sources JSONB, -- Array of source documents with scores
    model_used VARCHAR(100),
    tokens_used INTEGER,
    response_time_ms INTEGER,
    
    -- Feedback
    rating INTEGER CHECK (rating >= 1 AND rating <= 5),
    feedback TEXT,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW()
);

-- USER SETTINGS TABLE
CREATE TABLE IF NOT EXISTS user_settings (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    
    -- LLM Settings
    llm_provider VARCHAR(50) DEFAULT 'openai' CHECK (llm_provider IN ('openai', 'ollama')),
    llm_model VARCHAR(100) DEFAULT 'gpt-3.5-turbo',
    embedding_model VARCHAR(100) DEFAULT 'text-embedding-ada-002',
    
    -- RAG Settings
    chunk_size INTEGER DEFAULT 1000 CHECK (chunk_size >= 100 AND chunk_size <= 4000),
    chunk_overlap INTEGER DEFAULT 200 CHECK (chunk_overlap >= 0 AND chunk_overlap <= 1000),
    top_k_results INTEGER DEFAULT 5 CHECK (top_k_results >= 1 AND top_k_results <= 20),
    similarity_threshold FLOAT DEFAULT 0.7 CHECK (similarity_threshold >= 0 AND similarity_threshold <= 1),
    
    -- LLM Parameters
    temperature FLOAT DEFAULT 0.7 CHECK (temperature >= 0 AND temperature <= 1),
    max_tokens INTEGER DEFAULT 1000 CHECK (max_tokens >= 100 AND max_tokens <= 4000),
    
    -- UI Preferences
    theme VARCHAR(20) DEFAULT 'light' CHECK (theme IN ('light', 'dark', 'auto')),
    language VARCHAR(10) DEFAULT 'en',
    notes_view_mode VARCHAR(20) DEFAULT 'grid' CHECK (notes_view_mode IN ('grid', 'list')),
    default_note_folder_id UUID REFERENCES note_folders(id) ON DELETE SET NULL,
    
    -- Notification Preferences
    email_notifications BOOLEAN DEFAULT TRUE,
    processing_notifications BOOLEAN DEFAULT TRUE,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- NOTE COLLABORATORS TABLE (for sharing)
CREATE TABLE IF NOT EXISTS note_collaborators (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    note_id UUID NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- Permissions
    permission VARCHAR(20) DEFAULT 'view' CHECK (permission IN ('view', 'comment', 'edit', 'admin')),
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    accepted_at TIMESTAMP,
    
    -- Constraints
    UNIQUE(note_id, user_id)
);

-- NOTE LINKS TABLE (For bi-directional note linking)
CREATE TABLE IF NOT EXISTS note_links (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_note_id UUID NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    target_note_id UUID NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    
    -- Link metadata
    link_type VARCHAR(50) DEFAULT 'related' CHECK (link_type IN ('related', 'reference', 'parent', 'child')),
    description TEXT,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    
    -- Constraints
    UNIQUE(source_note_id, target_note_id),
    CHECK (source_note_id != target_note_id)
);

-- ACTIVITY LOG TABLE (for audit trail)
CREATE TABLE IF NOT EXISTS activity_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- Activity information
    action VARCHAR(50) NOT NULL, -- 'created', 'updated', 'deleted', 'viewed', 'shared'
    entity_type VARCHAR(50) NOT NULL, -- 'document', 'note', 'chat'
    entity_id UUID NOT NULL,
    
    -- Details
    details JSONB,
    ip_address INET,
    user_agent TEXT,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW()
);

-- INDICES FOR PERFORMANCE

-- Users
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC);

-- Documents
CREATE INDEX IF NOT EXISTS idx_documents_user_created ON documents(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_user_status ON documents(user_id, status);
CREATE INDEX IF NOT EXISTS idx_documents_file_type ON documents(file_type);
CREATE INDEX IF NOT EXISTS idx_documents_search ON documents USING gin(to_tsvector('english', title || ' ' || content));
CREATE INDEX IF NOT EXISTS idx_documents_tags ON documents USING gin(tags);

-- Document Chunks
CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id ON document_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_vector_id ON document_chunks(vector_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_content_search ON document_chunks USING gin(to_tsvector('english', content));

-- Note Folders
CREATE INDEX IF NOT EXISTS idx_note_folders_user_id ON note_folders(user_id);
CREATE INDEX IF NOT EXISTS idx_note_folders_parent_id ON note_folders(parent_folder_id);

-- Notes
CREATE INDEX IF NOT EXISTS idx_notes_user_created ON notes(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_folder_id ON notes(folder_id);
CREATE INDEX IF NOT EXISTS idx_notes_favorite ON notes(user_id, updated_at DESC) WHERE is_favorite = true;
CREATE INDEX IF NOT EXISTS idx_notes_archived ON notes(user_id, updated_at DESC) WHERE is_archived = true;
CREATE INDEX IF NOT EXISTS idx_notes_search ON notes USING gin(to_tsvector('english', title || ' ' || content));
CREATE INDEX IF NOT EXISTS idx_notes_linked_document ON notes(linked_document_id);
CREATE INDEX IF NOT EXISTS idx_notes_linked_chat ON notes(linked_chat_session_id);

-- Note Tags
CREATE UNIQUE INDEX IF NOT EXISTS idx_note_tags_user_name ON note_tags(user_id, name);


-- Note-Tag Relations
CREATE INDEX IF NOT EXISTS idx_note_tag_relations_note_id ON note_tag_relations(note_id);
CREATE INDEX IF NOT EXISTS idx_note_tag_relations_tag_id ON note_tag_relations(tag_id);

-- Chat Sessions
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_last_message ON chat_sessions(user_id, last_message_at DESC);

-- Chat Messages
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created ON chat_messages(session_id, created_at DESC);

-- Activity Logs
CREATE INDEX IF NOT EXISTS idx_activity_logs_user_created ON activity_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_logs_entity ON activity_logs(entity_type, entity_id);

-- FULL-TEXT SEARCH INDICES (GIN)
-- Combined search across documents
CREATE INDEX IF NOT EXISTS idx_documents_full_text ON documents 
USING gin(to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, '') || ' ' || coalesce(summary, '')));

-- Combined search across notes
CREATE INDEX IF NOT EXISTS idx_notes_full_text ON notes 
USING gin(to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, '') || ' ' || coalesce(summary, '')));

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply to tables
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_documents_updated_at BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_notes_updated_at BEFORE UPDATE ON notes
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_note_folders_updated_at BEFORE UPDATE ON note_folders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_chat_sessions_updated_at BEFORE UPDATE ON chat_sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_user_settings_updated_at BEFORE UPDATE ON user_settings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    
-- Function to update last_edited_at for notes
CREATE OR REPLACE FUNCTION update_note_last_edited()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.content IS DISTINCT FROM NEW.content THEN
        NEW.last_edited_at = NOW();
    END IF;
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_note_last_edited BEFORE UPDATE ON notes
    FOR EACH ROW EXECUTE FUNCTION update_note_last_edited();

-- Function to update chat session last_message_at
CREATE OR REPLACE FUNCTION update_chat_session_last_message()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE chat_sessions 
    SET last_message_at = NOW() 
    WHERE id = NEW.session_id;
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_chat_last_message AFTER INSERT ON chat_messages
    FOR EACH ROW EXECUTE FUNCTION update_chat_session_last_message();
    
-- Function to calculate word count
CREATE OR REPLACE FUNCTION calculate_word_count()
RETURNS TRIGGER AS $$
BEGIN
    NEW.word_count = array_length(regexp_split_to_array(trim(COALESCE(NEW.content, '')), '\s+'), 1);
    NEW.char_count = length(COALESCE(NEW.content, ''));
    NEW.content_preview = left(COALESCE(NEW.content, ''), 200);
    NEW.read_time_minutes = GREATEST(1, ROUND(NEW.word_count::numeric / 200)); -- 200 words per minute
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER calculate_note_word_count BEFORE INSERT OR UPDATE ON notes
    FOR EACH ROW WHEN (NEW.content IS NOT NULL)
    EXECUTE FUNCTION calculate_word_count();

-- Function to update template usage count
CREATE OR REPLACE FUNCTION increment_template_usage()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE note_templates 
    SET usage_count = usage_count + 1 
    WHERE id = NEW.parent_version_id;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- VIEWS FOR COMMON QUERIES
-- View: Notes with tag names
CREATE OR REPLACE VIEW notes_with_tags AS
SELECT 
    n.*,
    array_agg(t.name) FILTER (WHERE t.name IS NOT NULL) as tag_names,
    array_agg(t.color) FILTER (WHERE t.color IS NOT NULL) as tag_colors
FROM notes n
LEFT JOIN note_tag_relations ntr ON n.id = ntr.note_id
LEFT JOIN note_tags t ON ntr.tag_id = t.id
GROUP BY n.id;

-- View: Documents with statistics
CREATE OR REPLACE VIEW documents_with_stats AS
SELECT 
    d.*,
    COUNT(dc.id) as total_chunks,
    u.email as owner_email,
    u.full_name as owner_name
FROM documents d
LEFT JOIN document_chunks dc ON d.id = dc.document_id
LEFT JOIN users u ON d.user_id = u.id
GROUP BY d.id, u.email, u.full_name;

-- View: Folder hierarchy with note counts
CREATE OR REPLACE VIEW folders_with_counts AS
SELECT 
    f.*,
    COUNT(n.id) as note_count,
    COUNT(cf.id) as subfolder_count
FROM note_folders f
LEFT JOIN notes n ON f.id = n.folder_id AND n.is_archived = FALSE
LEFT JOIN note_folders cf ON f.id = cf.parent_folder_id
GROUP BY f.id;

-- View: Recent activity
CREATE OR REPLACE VIEW recent_activity AS
SELECT 
    'document' as type,
    id,
    user_id,
    title,
    created_at as activity_time
FROM documents
UNION ALL
SELECT 
    'note' as type,
    id,
    user_id,
    title,
    created_at as activity_time
FROM notes
UNION ALL
SELECT 
    'chat' as type,
    id,
    user_id,
    title,
    created_at as activity_time
FROM chat_sessions
ORDER BY activity_time DESC;

-- Function to get user statistics
CREATE OR REPLACE FUNCTION get_user_stats(p_user_id UUID)
RETURNS JSON AS $$
DECLARE
    result JSON;
BEGIN
    SELECT json_build_object(
        'total_documents', (SELECT COUNT(*) FROM documents WHERE user_id = p_user_id),
        'total_notes', (SELECT COUNT(*) FROM notes WHERE user_id = p_user_id AND is_archived = FALSE),
        'total_chats', (SELECT COUNT(*) FROM chat_sessions WHERE user_id = p_user_id),
        'favorite_notes', (SELECT COUNT(*) FROM notes WHERE user_id = p_user_id AND is_favorite = TRUE),
        'recent_documents', (SELECT COUNT(*) FROM documents WHERE user_id = p_user_id AND created_at > NOW() - INTERVAL '7 days'),
        'recent_notes', (SELECT COUNT(*) FROM notes WHERE user_id = p_user_id AND created_at > NOW() - INTERVAL '7 days')
    ) INTO result;
    
    RETURN result;
END;
$$ LANGUAGE plpgsql;

-- Function to search across all content
CREATE OR REPLACE FUNCTION search_all_content(
    p_user_id UUID,
    p_query TEXT,
    p_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    type TEXT,
    id UUID,
    title TEXT,
    snippet TEXT,
    relevance REAL,
    created_at TIMESTAMP
) AS $$
BEGIN
    RETURN QUERY
    (
        SELECT 
            'document'::TEXT as type,
            d.id,
            d.title,
            LEFT(d.content, 200)::TEXT as snippet,
            ts_rank(to_tsvector('english', d.title || ' ' || COALESCE(d.content, '')), plainto_tsquery('english', p_query)) as relevance,
            d.created_at
        FROM documents d
        WHERE d.user_id = p_user_id
        AND to_tsvector('english', d.title || ' ' || COALESCE(d.content, '')) @@ plainto_tsquery('english', p_query)
        
        UNION ALL
        
        SELECT 
            'note'::TEXT as type,
            n.id,
            n.title,
            LEFT(n.content, 200)::TEXT as snippet,
            ts_rank(to_tsvector('english', n.title || ' ' || n.content), plainto_tsquery('english', p_query)) as relevance,
            n.created_at
        FROM notes n
        WHERE n.user_id = p_user_id
        AND n.is_archived = FALSE
        AND to_tsvector('english', n.title || ' ' || n.content) @@ plainto_tsquery('english', p_query)
        
        ORDER BY relevance DESC
        LIMIT p_limit
    );
END;
$$ LANGUAGE plpgsql;