"""Bounded native presentation of KM view payloads; no inferred membership or edges."""
from PySide6.QtCore import Qt,Signal
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QTreeWidget,QTreeWidgetItem,
    QHBoxLayout,QPushButton,QLabel)
from .km_session import display_row
from .navigation import reference
from .record_content import connection_caption,display_value
from .relationship_evidence import RelationshipEvidence,authority_label

ROLE=Qt.ItemDataRole.UserRole


class KnowledgeView(QWidget):
    """Canonical grouped and chronological results; spatial context uses Neighborhood."""
    selected_record=Signal(str)
    selected_conversation=Signal(object)
    def __init__(self,parent=None):
        super().__init__(parent);self.labels={};layout=QVBoxLayout(self);layout.setContentsMargins(5,5,5,5)
        self.notice=QLabel();self.notice.setTextFormat(Qt.TextFormat.PlainText);self.notice.setWordWrap(True);layout.addWidget(self.notice)
        self.tree=QTreeWidget();self.tree.setHeaderLabels(['Record / grouping','Evidence / context']);self.tree.setColumnWidth(0,430);layout.addWidget(self.tree,1)
        self.tree.itemActivated.connect(self.open_item)
        row=QHBoxLayout()
        proof=QPushButton('Inspect selected evidence');proof.clicked.connect(self.inspect);row.addWidget(proof);row.addStretch();layout.addLayout(row)
    def node(self,parent,title,authority='',data=None,rid=None,kind=None):
        item=QTreeWidgetItem([str(title),str(authority)])
        item.setData(0,ROLE,{'record_id':rid,'evidence':data});item.setToolTip(0,str(title))
        if not kind and isinstance(data,dict):
            kind='WorkspaceOrganization' if data.get('effective_kind')=='workspace_overlay' else data.get('record_type')
        item.setData(0,ROLE+2,reference({'record_id':rid,'record_type':kind}))
        (parent.addChild(item) if parent else self.tree.addTopLevelItem(item));return item
    def open_item(self,item,*_):
        rid=(item.data(0,ROLE) or {}).get('record_id')
        if rid:self.selected_record.emit(rid)
        else:
            evidence=(item.data(0,ROLE) or {}).get('evidence')
            if isinstance(evidence,dict) and 'messages' in evidence:
                self.selected_conversation.emit({'mailbox_id':evidence['mailbox_id'],'conversation_id':evidence['conversation_id']})
    def inspect(self):
        item=self.tree.currentItem()
        if not item:return
        d=RelationshipEvidence(item.text(0),(item.data(0,ROLE) or {}).get('evidence'),self.labels,self);d.exec()
    def name(self,rid):return self.labels.get(rid,{}).get('label') or 'Record label unavailable'
    def value(self,value):
        if isinstance(value,dict) and value.get('record_id'):return self.name(value['record_id'])
        if isinstance(value,list):return ', '.join(self.value(v) for v in value) or 'No values supplied'
        return display_value(value)
    def set_payload(self,data,view_kind='',*,labels=None):
        self.tree.clear();self.labels=labels or {}
        def record(row,parent=None):
            rid=row.get('record_id');title=display_row(row)['label'] if row.get('properties') or row.get('display_name') else row.get('name',rid)
            node=self.node(parent,title or 'Record',row.get('record_type','Evidence-backed context'),row,rid)
            direct=row.get('verified_connections',[])+row.get('verified_organization_edges',[])
            for edge in direct:
                if not isinstance(edge,dict):continue
                outgoing=edge.get('source_id')==rid;other=edge.get('target_id') if outgoing else edge.get('source_id')
                kind=edge.get('target_type') if outgoing else edge.get('source_type')
                caption=connection_caption(row.get('record_type'),dict(edge,direction='outgoing' if outgoing else 'incoming'))
                child=self.node(node,caption+' · '+self.name(other),authority_label(edge.get('authority')),edge,other,kind)
                child.setData(0,ROLE+3,str(edge.get('predicate'))+':'+str(other))
            for field,title in (('workspace_role','Role'),('key_contact','Key contact'),('enterprise','Enterprise')):
                if row.get(field) is not None:self.node(node,title+': '+self.value(row[field]),'User-owned interpretation',row[field])
            parent_ref=row.get('parent_command')
            if isinstance(parent_ref,dict) and parent_ref.get('record_id'):
                target=parent_ref['record_id']
                edge={'source_id':rid,'target_id':target,'predicate':'ws:parent_command','authority':'workspace_manual'}
                self.node(node,'Parent command · '+self.name(target),'Explicit user-owned reference',edge,target,parent_ref.get('record_type'))
            for ref in row.get('properties',{}).get('ws:poc_people',[]) or []:
                if isinstance(ref,dict):self.node(node,'Point of contact · '+self.name(ref.get('record_id')),'Explicit user-owned reference',ref,ref.get('record_id'))
            for anchor in row.get('source_heading_anchors',[]):self.node(node,anchor.get('text','Heading'),'Source document structure (not a semantic relation)',anchor)
            for pid,value in row.get('workspace_properties',{}).items():
                self.node(node,value.get('label') or self.labels.get(pid,{}).get('label') or 'Workspace field','Workspace · '+str(value.get('state','')).replace('_',' ')+' · '+self.value(value.get('value')),value)
            return node
        sections=data.get('sections')
        if sections is not None:
            for title,rows in sections.items():
                group=self.node(None,title+' ('+str(len(rows))+')','Connected records')
                for row in rows:
                    if row.get('role'):
                        child=self.node(group,row['role'],'Manual role grouping',row)
                        for rid in row.get('person_ids',[]):self.node(child,self.name(rid),'Person',{'record_id':rid},rid,'Person')
                    else:record(row,group)
        else:
            group_nodes={}
            for row in data.get('rows',[]):
                if 'messages' in row:
                    messages=row['messages'];title=display_row(messages[0])['label'] if messages else 'Conversation'
                    group=self.node(None,title+' · '+str(row.get('count',len(messages)))+' messages','Source conversation grouping',row)
                    group.setData(0,ROLE+3,str(row.get('mailbox_id'))+':'+str(row.get('conversation_id')))
                    for msg in row['messages']:record(msg,group)
                    if row.get('messages_truncated') and not data.get('message_page'):
                        self.node(group,str(row['count'])+' captured messages — open this conversation to page through all of them.','Bounded preview')
                else:
                    group_keys=[]
                    if data.get('kind')=='enterprise_map':group_keys=[str(row.get('enterprise') or 'Unassigned enterprise')]
                    elif data.get('kind')=='by_org':group_keys=row.get('effective_organization_ids') or ['Unmatched people']
                    elif data.get('kind') in ('by_author','by_loe'):group_keys=sorted({e['target_id'] for e in row.get('verified_connections',[])})
                    for key in group_keys:
                        if key not in group_nodes:
                            title=self.name(key) if key in self.labels or key.startswith(('org_','wsorg_','person_','loe_')) else key
                            group_nodes[key]=self.node(None,title,'Evidence grouping',{'grouping':title,'description':'Records grouped by the canonical view. Supporting relationships are shown beneath each record.','group_id':key})
                            group_nodes[key].setData(0,ROLE+3,key)
                        record(row,group_nodes[key])
                    if not group_keys:record(row)
        self.tree.expandToDepth(0)
        status=data.get('state','available');notice='' if status=='available' else status.replace('_',' ').capitalize()+'.'
        if data.get('reason'):notice+=' '+str(data['reason'])
        if data.get('next_cursor'):notice+=' More results are available using Next.'
        self.notice.setText(notice.strip());self.notice.setVisible(bool(notice.strip()))
