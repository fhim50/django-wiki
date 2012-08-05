# -*- coding: utf-8 -*-
from django.shortcuts import render_to_response, redirect, get_object_or_404
from django.template.context import RequestContext
from django.contrib.auth.decorators import permission_required
from django.utils.translation import ugettext as _

from wiki import models
from wiki import forms
from wiki import editors
from wiki.conf import settings

from django.contrib import messages
from django.views.generic.list import ListView
import difflib
from wiki.decorators import json_view
from django.utils.decorators import method_decorator
from django.views.generic.edit import FormView

from wiki.decorators import get_article
from django.views.generic.base import TemplateView

from wiki.core import plugins_registry
from wiki.core.diff import simple_merge

@get_article(can_read=True)
def preview(request, article, urlpath=None, template_file="wiki/preview_inline.html"):
    
    content = article.current_revision.content
    title = article.current_revision.title
    
    revision_id = request.GET.get('revision_id', None)
    revision = None
    
    if request.method == 'POST':
        edit_form = forms.EditForm(article.current_revision, request.POST, preview=True)
        if edit_form.is_valid():
            title = edit_form.cleaned_data['title']
            content = edit_form.cleaned_data['content']
    
    elif revision_id:
        revision = get_object_or_404(models.ArticleRevision, article=article, id=revision_id)
        title = revision.title
        content = revision.content
    
    c = RequestContext(request, {'urlpath': urlpath,
                                 'article': article,
                                 'title': title,
                                 'revision': revision,
                                 'content': content})
    return render_to_response(template_file, c)

@get_article(can_read=True)
def root(request, article, template_file="wiki/article.html", urlpath=None):
    
    c = RequestContext(request, {'urlpath': urlpath,
                                 'article': article,})
    return render_to_response(template_file, c)

class Edit(FormView):
    
    form_class = forms.EditForm
    template_name="wiki/edit.html"
    
    @method_decorator(get_article(can_write=True))
    def dispatch(self, request, article, *args, **kwargs):
        self.urlpath = kwargs.pop('urlpath', None)
        self.article = article
        return super(Edit, self).dispatch(request, *args, **kwargs)
    
    def get_form(self, form_class):
        """
        Returns an instance of the form to be used in this view.
        """
        return form_class(self.article.current_revision, **self.get_form_kwargs())
    
    def form_valid(self, form):
        revision = models.ArticleRevision()
        revision.inherit_predecessor(self.article)
        revision.title = form.cleaned_data['title']
        revision.content = form.cleaned_data['content']
        revision.user_message = form.cleaned_data['summary']
        if not self.request.user.is_anonymous:
            revision.user = self.request.user
            if settings.LOG_IPS_USERS:
                revision.ip_address = self.request.META.get('REMOTE_ADDR', None)
        elif settings.LOG_IPS_ANONYMOUS:
            revision.ip_address = self.request.META.get('REMOTE_ADDR', None)
        self.article.add_revision(revision)
        messages.success(self.request, _(u'A new revision of the article was succesfully added.'))
        return self.get_success_url()
    
    def get_success_url(self):
        if not self.urlpath is None:
            return redirect("wiki:get_url", self.urlpath.path)
        # TODO: Where to go if it's a different object? It's probably
        # an ajax callback, so we don't care... but should perhaps return
        # a status
        return
    
    def get_context_data(self, **kwargs):
        kwargs['urlpath'] = self.urlpath
        kwargs['article'] = self.article
        kwargs['edit_form'] = kwargs.pop('form', None)
        kwargs['editor'] = editors.editor
        return super(Edit, self).get_context_data(**kwargs)


class Create(FormView):
    
    form_class = forms.CreateForm
    template_name="wiki/create.html"
    
    @method_decorator(get_article(can_write=True))
    def dispatch(self, request, article, *args, **kwargs):
        self.urlpath = kwargs.pop('urlpath', None)
        self.article = article
        return super(Create, self).dispatch(request, *args, **kwargs)
    
    def get_form(self, form_class):
        """
        Returns an instance of the form to be used in this view.
        """
        kwargs = self.get_form_kwargs()
        initial = kwargs.get('initial', {})
        initial['slug'] = self.request.GET.get('slug', None)
        kwargs['initial'] = initial
        return form_class(self.urlpath, **kwargs)
    
    def form_valid(self, form):
        user=None
        ip_address = None
        if not self.request.user.is_anonymous:
            user = self.request.user
            if settings.LOG_IPS_USERS:
                ip_address = self.request.META.get('REMOTE_ADDR', None)
        elif settings.LOG_IPS_ANONYMOUS:
            ip_address = self.request.META.get('REMOTE_ADDR', None)
        self.newpath = models.URLPath.create_article(self.urlpath,
                                                     form.cleaned_data['slug'],
                                                     title=form.cleaned_data['title'],
                                                     content=form.cleaned_data['content'],
                                                     user_message=form.cleaned_data['summary'],
                                                     user=user,
                                                     ip_address=ip_address)
        messages.success(self.request, _(u"New article '%s' created.") % self.newpath.article.title)
        return self.get_success_url()
    
    def get_success_url(self):
        return redirect('wiki:get_url', self.newpath.path)
    
    def get_context_data(self, **kwargs):
        kwargs['parent_urlpath'] = self.urlpath
        kwargs['parent_article'] = self.article
        kwargs['create_form'] = kwargs.pop('form', None)
        kwargs['editor'] = editors.editor
        return super(Create, self).get_context_data(**kwargs)
    
class Settings(TemplateView):
    
    permission_form_class = forms.PermissionsForm
    template_name="wiki/settings.html"
    
    @method_decorator(get_article(can_read=True))
    def dispatch(self, request, article, *args, **kwargs):
        self.urlpath = kwargs.pop('urlpath', None)
        self.article = article
        return super(Settings, self).dispatch(request, *args, **kwargs)
    
    def get_form_classes(self,):
        """
        Return all settings forms that can be filled in
        """
        settings_forms = [F for F in plugins_registry._settings_forms]
        if (self.request.user and self.request.user.is_superuser or 
            self.article.owner == self.request.user):
            settings_forms.append(self.permission_form_class)
        settings_forms.sort(key=lambda form: form.settings_order)
        for i in range(len(settings_forms)):
            setattr(settings_forms[i], 'action', 'form%d' % i)
        return settings_forms
    
    def post(self, *args, **kwargs):
        self.forms = []
        for Form in self.get_form_classes():
            if Form.action == self.request.GET.get('f', None):
                form = Form(self.article, self.request.user,self.request.POST)
                if form.is_valid():
                    form.save()
                    usermessage = form.get_usermessage()
                    if usermessage:
                        messages.success(self.request, usermessage)
                    return redirect('wiki:settings_url', self.urlpath.path)
            else:
                form = Form(self.article, self.request.user)
            self.forms.append(form)
        return super(Settings, self).get(*args, **kwargs)
    
    def get(self, *args, **kwargs):
        self.forms = []
        for Form in self.get_form_classes():
            self.forms.append(Form(self.article, self.request.user))
        return super(Settings, self).get(*args, **kwargs)

    def get_success_url(self):
        return redirect('wiki:settings_url', self.urlpath.path)
    
    def get_context_data(self, **kwargs):
        kwargs['urlpath'] = self.urlpath
        kwargs['article'] = self.article
        kwargs['forms'] = self.forms
        return kwargs

class History(ListView):
    
    template_name="wiki/history.html"
    allow_empty = True
    context_object_name = 'revisions'
    paginate_by = 10
    
    def get_queryset(self):
        return models.ArticleRevision.objects.filter(article=self.article).order_by('-created')
    
    def get_context_data(self, **kwargs):
        kwargs['urlpath'] = self.urlpath
        kwargs['article'] = self.article
        return super(History, self).get_context_data(**kwargs)
    
    @method_decorator(get_article(can_read=True))
    def dispatch(self, request, article, *args, **kwargs):
        self.urlpath = kwargs.pop('urlpath', None)
        self.article = article
        return super(History, self).dispatch(request, *args, **kwargs)

@get_article(can_write=True)
def change_revision(request, article, revision_id=None, urlpath=None):
    revision = get_object_or_404(models.ArticleRevision, article=article, id=revision_id)
    article.current_revision = revision
    article.save()
    messages.success(request, _(u'The article %s is now set to display revision #%d') % (revision.title, revision.revision_number))
    if urlpath:
        return redirect("wiki:history_url", urlpath.path)
    else:
        # TODO: Where to go if not a urlpath object?
        pass
    
@permission_required('wiki.add_article')
def root_create(request):
    if request.method == 'POST':
        create_form = forms.CreateRoot(request.POST)
        if create_form.is_valid():
            root = models.URLPath.create_root(title=create_form.cleaned_data["title"],
                                              content=create_form.cleaned_data["content"])
            return redirect("wiki:root")
    else:
        create_form = forms.CreateRoot()
    
    c = RequestContext(request, {'create_form': create_form,
                                 'editor': editors.editor,})
    return render_to_response("wiki/article/create_root.html", c)

@get_article(can_read=True)
def get_url(request, article, template_file="wiki/article.html", urlpath=None):
    
    c = RequestContext(request, {'urlpath': urlpath,
                                 'article': article,})
    return render_to_response(template_file, c)

@json_view
def diff(request, revision_id, other_revision_id=None):
    
    revision = get_object_or_404(models.ArticleRevision, id=revision_id)
    
    if not other_revision_id:
        other_revision = revision.previous_revision
    
    baseText = other_revision.content if other_revision else ""
    newText = revision.content
    
    differ = difflib.Differ(charjunk=difflib.IS_CHARACTER_JUNK)
    diff = differ.compare(baseText.splitlines(1), newText.splitlines(1))
    
    other_changes = []
    
    if not other_revision or other_revision.title != revision.title:
        other_changes.append((_(u'New title'), revision.title))
    
    return dict(diff=list(diff), other_changes=other_changes)

@get_article(can_write=True)
def merge(request, article, revision_id, urlpath=None, template_file="wiki/preview_inline.html", preview=False):
    
    revision = get_object_or_404(models.ArticleRevision, article=article, id=revision_id)
    
    current_text = article.current_revision.content if article.current_revision else ""
    new_text = revision.content
    
    content = simple_merge(current_text, new_text)
    
    # Save new revision
    if not preview:
        old_revision = article.current_revision
        new_revision = models.ArticleRevision()
        new_revision.inherit_predecessor(article)
        new_revision.title=article.current_revision.title
        new_revision.content=content
        new_revision.automatic_log = (_(u'Merge between Revision #%(r1)d and Revision #%(r2)d') % 
                                      {'r1': revision.revision_number, 
                                       'r2': old_revision.revision_number})
        article.add_revision(new_revision, save=True)
        messages.success(request, _(u'A new revision was created: Merge between Revision #%(r1)d and Revision #%(r2)d') % 
                         {'r1': revision.revision_number,
                          'r2': old_revision.revision_number})
        if urlpath:
            return redirect('wiki:edit_url', urlpath.path)
        
    
    c = RequestContext(request, {'article': article,
                                 'title': article.current_revision.title,
                                 'revision': None,
                                 'merge1': revision,
                                 'merge2': article.current_revision,
                                 'merge': True,
                                 'content': content})
    return render_to_response(template_file, c)

