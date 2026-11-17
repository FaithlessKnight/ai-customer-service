from pymilvus import MilvusClient
mc = MilvusClient(uri='http://192.168.4.199:19530', token='admin:milvus@2026')
cols = mc.list_collections()
print('collections:', cols)
for col in cols:
    stats = mc.get_collection_stats(col)
    print(f'  {col}: row_count={stats["row_count"]}')
