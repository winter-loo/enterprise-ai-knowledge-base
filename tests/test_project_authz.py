from authz import store


def test_fixed_project_roles_define_the_document_write_boundary():
    assert store.role_allows("viewer", "document:write") is False
    assert store.role_allows("editor", "document:write") is True
    assert store.role_allows("manager", "document:write") is True
