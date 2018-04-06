from __future__ import unicode_literals

import logging
import warnings

from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.encoding import force_text
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.translation import get_language, ugettext_lazy as _
from django.views.generic import TemplateView, View
from fred_idl.Registry.PublicRequest import HAS_DIFFERENT_BLOCK, INVALID_EMAIL, OBJECT_ALREADY_BLOCKED, \
    OBJECT_NOT_BLOCKED, OBJECT_NOT_FOUND, OBJECT_TRANSFER_PROHIBITED, OPERATION_PROHIBITED, Language, LockRequestType

from webwhois.forms import BlockObjectForm, SendPasswordForm, UnblockObjectForm
from webwhois.forms.public_request import LOCK_TYPE_ALL, LOCK_TYPE_TRANSFER, LOCK_TYPE_URL_PARAM, SEND_TO_CUSTOM, \
    SEND_TO_IN_REGISTRY
from webwhois.utils.corba_wrapper import LOGGER, PUBLIC_REQUEST
from webwhois.utils.public_response import BlockResponse, SendPasswordResponse
from webwhois.views.base import BaseContextMixin
from webwhois.views.public_request_mixin import PublicRequestFormView, PublicRequestKnownException, \
    PublicRequestLoggerMixin

WEBWHOIS_LOGGING = logging.getLogger(__name__)


class SendPasswordFormView(BaseContextMixin, PublicRequestFormView):
    """Send password (AuthInfo) view."""

    form_class = SendPasswordForm
    template_name = 'webwhois/form_send_password.html'
    form_cleaned_data = None

    def _get_logging_request_name_and_properties(self, data):
        """Return Request type name and Properties."""
        properties_in = [
            ("handle", data["handle"]),
            ("handleType", data['object_type']),
            ("confirmMethod", data['confirmation_method']),
            ("sendTo", data['send_to']),
        ]
        custom_email = data.get("custom_email")
        if custom_email:
            properties_in.append(("customEmail", custom_email))
        return "AuthInfo", properties_in

    def _call_registry_command(self, form, log_request_id):
        data = form.cleaned_data
        try:
            if data['send_to'] == 'custom_email':
                response_id = PUBLIC_REQUEST.create_authinfo_request_non_registry_email(
                    self._get_object_type(data['object_type']), data['handle'], log_request_id,
                    self._get_confirmed_by_type(data['confirmation_method']), data['custom_email'])
            else:
                # confirm_type_name is 'signed_email'
                response_id = PUBLIC_REQUEST.create_authinfo_request_registry_email(
                    self._get_object_type(data['object_type']), data['handle'], log_request_id)
        except OBJECT_NOT_FOUND as err:
            form.add_error('handle',
                           _('Object not found. Check that you have correctly entered the Object type and Handle.'))
            raise PublicRequestKnownException(type(err).__name__)
        except OBJECT_TRANSFER_PROHIBITED as err:
            form.add_error('handle', _('Transfer of object is prohibited. The request can not be accepted.'))
            raise PublicRequestKnownException(type(err).__name__)
        except INVALID_EMAIL as err:
            form.add_error('custom_email', _('The email was not found or the address is not valid.'))
            raise PublicRequestKnownException(type(err).__name__)
        return response_id

    def get_public_response(self, form, public_request_id):
        request_type = self._get_logging_request_name_and_properties(form.cleaned_data)[0]
        if form.cleaned_data['send_to'] == 'custom_email':
            custom_email = form.cleaned_data['custom_email']
        else:
            custom_email = None
        return SendPasswordResponse(form.cleaned_data['object_type'], public_request_id, request_type,
                                    form.cleaned_data['handle'], custom_email)

    def get_initial(self):
        data = super(SendPasswordFormView, self).get_initial()
        data["handle"] = self.request.GET.get("handle")
        data["object_type"] = self.request.GET.get("object_type")
        send_to = self.request.GET.get("send_to")
        if send_to and send_to in (SEND_TO_IN_REGISTRY, SEND_TO_CUSTOM):
            data["send_to"] = send_to
        return data

    def get_success_url(self):
        if self.success_url:
            return force_text(self.success_url)
        url_name = "webwhois:response_not_found"
        if self.form_cleaned_data['confirmation_method'] == 'notarized_letter':
            url_name = 'webwhois:notarized_letter_response'
        else:
            if self.form_cleaned_data['send_to'] == 'email_in_registry':
                url_name = 'webwhois:email_in_registry_response'
            else:
                url_name = 'webwhois:custom_email_response'
        return reverse(url_name, kwargs={'public_key': self.public_key},
                       current_app=self.request.resolver_match.namespace)


class BlockUnblockFormView(PublicRequestFormView):
    """Block or Unblock object form view."""

    form_class = None
    block_action = None
    logging_lock_type = None
    form_cleaned_data = None

    def _get_lock_type(self, key):
        raise NotImplementedError

    def _get_logging_request_name_and_properties(self, data):
        """Return Request type name and Properties."""
        lock_type_key = self.logging_lock_type[data['lock_type']]
        properties_in = [
            ("handle", data["handle"]),
            ("handleType", data['object_type']),
            ("confirmMethod", data['confirmation_method']),
        ]
        return lock_type_key, properties_in

    def _call_registry_command(self, form, log_request_id):
        response_id = None
        try:
            response_id = PUBLIC_REQUEST.create_block_unblock_request(
                self._get_object_type(form.cleaned_data['object_type']), form.cleaned_data['handle'], log_request_id,
                self._get_confirmed_by_type(form.cleaned_data['confirmation_method']),
                self._get_lock_type(form.cleaned_data['lock_type']))
        except OBJECT_NOT_FOUND as err:
            form.add_error('handle', _('Object not found. Check that you have correctly entered the Object type and '
                                       'Handle.'))
            raise PublicRequestKnownException(type(err).__name__)
        except OBJECT_ALREADY_BLOCKED as err:
            form.add_error('handle', _('This object is already blocked. The request can not be accepted.'))
            raise PublicRequestKnownException(type(err).__name__)
        except OBJECT_NOT_BLOCKED as err:
            form.add_error('handle', _('This object is not blocked. The request can not be accepted.'))
            raise PublicRequestKnownException(type(err).__name__)
        except HAS_DIFFERENT_BLOCK as err:
            form.add_error('handle', _('This object has another active blocking. The request can not be accepted.'))
            raise PublicRequestKnownException(type(err).__name__)
        except OPERATION_PROHIBITED as err:
            form.add_error('handle', _('Operation for this object is prohibited. The request can not be accepted.'))
            raise PublicRequestKnownException(type(err).__name__)
        return response_id

    def get_public_response(self, form, public_request_id):
        request_type = self._get_logging_request_name_and_properties(form.cleaned_data)[0]
        return BlockResponse(form.cleaned_data['object_type'], public_request_id, request_type,
                             form.cleaned_data['handle'], self.block_action, form.cleaned_data['lock_type'])

    def get_initial(self):
        data = super(BlockUnblockFormView, self).get_initial()
        data["handle"] = self.request.GET.get("handle")
        data["object_type"] = self.request.GET.get("object_type")
        lock_type = self.request.GET.get(LOCK_TYPE_URL_PARAM)
        if lock_type and lock_type in (LOCK_TYPE_TRANSFER, LOCK_TYPE_ALL):
            data["lock_type"] = lock_type
        return data

    def get_success_url(self):
        if self.success_url:
            return force_text(self.success_url)
        if self.form_cleaned_data['confirmation_method'] == 'notarized_letter':
            url_name = 'webwhois:notarized_letter_response'
        else:
            url_name = 'webwhois:custom_email_response'
        return reverse(url_name, kwargs={'public_key': self.public_key},
                       current_app=self.request.resolver_match.namespace)


class BlockObjectFormView(BaseContextMixin, BlockUnblockFormView):
    """Block object form view."""

    form_class = BlockObjectForm
    template_name = 'webwhois/form_block_object.html'
    block_action = 'block'
    logging_lock_type = {
        LOCK_TYPE_TRANSFER: "BlockTransfer",
        LOCK_TYPE_ALL: "BlockChanges",
    }

    def _get_lock_type(self, key):
        return {
            LOCK_TYPE_TRANSFER: LockRequestType.block_transfer,
            LOCK_TYPE_ALL: LockRequestType.block_transfer_and_update,
        }[key]


class UnblockObjectFormView(BaseContextMixin, BlockUnblockFormView):
    """Unblock object form view."""

    form_class = UnblockObjectForm
    template_name = 'webwhois/form_unblock_object.html'
    block_action = 'unblock'
    logging_lock_type = {
        LOCK_TYPE_TRANSFER: "UnblockTransfer",
        LOCK_TYPE_ALL: "UnblockChanges",
    }

    def _get_lock_type(self, key):
        return {
            LOCK_TYPE_TRANSFER: LockRequestType.unblock_transfer,
            LOCK_TYPE_ALL: LockRequestType.unblock_transfer_and_update,
        }[key]


class PublicResponseNotFound(Exception):
    """Public response was not found in the cache."""


class ResponseDataKeyMissing(PublicResponseNotFound):
    """Exception for a situation when the response data dict does not have required key."""

    def __init__(self, *args, **kwargs):
        warnings.warn("ResponseDataKeyMissing is deprecated in favor of PublicResponseNotFound.",
                      DeprecationWarning)
        super(ResponseDataKeyMissing, self).__init__(*args, **kwargs)


class PublicResponseNotFoundView(BaseContextMixin, TemplateView):
    """Response Not found view."""

    template_name = 'webwhois/public_request_response_not_found.html'


class ResponseNotFoundView(PublicResponseNotFoundView):
    """Backwards compatible view for PublicResponseNotFoundView."""

    def __init__(self, *args, **kwargs):
        warnings.warn("ResponseNotFoundView is deprecated in favor of PublicResponseNotFoundView.",
                      DeprecationWarning)
        super(ResponseNotFoundView, self).__init__(*args, **kwargs)


class BaseResponseTemplateView(BaseContextMixin, TemplateView):
    """Base view for public request responses."""

    def __init__(self, *args, **kwargs):
        super(BaseResponseTemplateView, self).__init__(*args, **kwargs)
        self._public_response = None

    def get(self, request, *args, **kwargs):
        try:
            self.get_public_response()
        except PublicResponseNotFound:
            return HttpResponseRedirect(reverse("webwhois:response_not_found",
                                                kwargs={"public_key": kwargs['public_key']},
                                                current_app=self.request.resolver_match.namespace))
        return super(BaseResponseTemplateView, self).get(request, *args, **kwargs)

    def get_public_response(self):
        """Return relevant public response."""
        # Cache the result for case the cache gets deleted while handling the request.
        if self._public_response is None:
            public_key = self.kwargs['public_key']
            public_response = cache.get(public_key)
            if public_response is None:
                raise PublicResponseNotFound(public_key)
            if isinstance(public_response, dict):
                warnings.warn(
                    "Storing responses to public requests as dicts is deprecated, use PublicResponse instead.",
                    DeprecationWarning)
                data = public_response
                if 'send_to' in data:
                    public_response = SendPasswordResponse(data['object_type'], data['response_id'],
                                                           data.get('request_name'), data['handle'],
                                                           data['custom_email'])
                else:
                    public_response = BlockResponse(data['object_type'], data['response_id'], data.get('request_name'),
                                                    data['handle'], data['block_unblock_action_type'],
                                                    data['lock_type'])
                public_response.create_date = data['created_date']
            self._public_response = public_response
        return self._public_response


class TextSendPasswordMixin(object):
    """Texts shared by all Request for password response views."""

    text_title = {
        'contact': _('Request for password for transfer contact %(handle)s'),
        'domain': _('Request for password for transfer domain name %(handle)s'),
        'nsset': _('Request for password for transfer nameserver set %(handle)s'),
        'keyset': _('Request for password for transfer keyset %(handle)s'),
    }


class EmailInRegistryView(TextSendPasswordMixin, BaseResponseTemplateView):
    """Email in registy view."""

    template_name = 'webwhois/public_request_email_in_registry.html'

    text_content = {
        'contact': _('We received successfully your request for a password to change the contact '
                     '<strong>{handle}</strong> sponsoring registrar. An email with the password will be sent '
                     'to the email address from the registry.'),
        'domain': _('We received successfully your request for a password to change the domain '
                    '<strong>{handle}</strong> sponsoring registrar. An email with the password will be sent '
                    'to the email address of domain holder and admin contacts.'),
        'nsset': _('We received successfully your request for a password to change the nameserver set '
                   '<strong>{handle}</strong> sponsoring registrar. An email with the password will be sent '
                   'to the email addresses of the nameserver set\'s technical contacts.'),
        'keyset': _('We received successfully your request for a password to change the keyset '
                    '<strong>{handle}</strong> sponsoring registrar. An email with the password will be sent '
                    'to the email addresses of the keyset\'s technical contacts.'),
    }

    def get_context_data(self, **kwargs):
        context = super(EmailInRegistryView, self).get_context_data(**kwargs)
        public_response = self.get_public_response()
        title = self.text_title[public_response.object_type] % {'handle': public_response.handle}
        context['text_title'] = context['text_header'] = title
        context['text_content'] = format_html(self.text_content[public_response.object_type],
                                              handle=public_response.handle)
        return context


class TextPasswordAndBlockMixin(TextSendPasswordMixin):
    """Texts shared by Custom e-mail view and Notarized letter view."""

    text_title = {
        'send_password': TextSendPasswordMixin.text_title,
        'block_transfer': {
            'contact': _('Request for blocking of contact %(handle)s'),
            'domain': _('Request for blocking of domain name %(handle)s'),
            'nsset': _('Request for blocking of nameserver set %(handle)s'),
            'keyset': _('Request for blocking of keyset %(handle)s'),
        },
        'block': {
            'contact': _('Request for blocking of contact %(handle)s'),
            'domain': _('Request for blocking of domain name %(handle)s'),
            'nsset': _('Request for blocking of nameserver set %(handle)s'),
            'keyset': _('Request for blocking of keyset %(handle)s'),
        },
        'unblock': {
            'contact': _('Request to cancel the blocking of contact %(handle)s'),
            'domain': _('Request to cancel the blocking of domain name %(handle)s'),
            'nsset': _('Request to cancel the blocking of nameserver set %(handle)s'),
            'keyset': _('Request to cancel the blocking of keyset %(handle)s'),
        },
    }


class CustomEmailView(TextPasswordAndBlockMixin, BaseResponseTemplateView):
    """Custom email view."""

    template_name = 'webwhois/public_request_custom_email.html'

    text_subject = {
        'send_password': {
            'contact': _('Subject: Request for password for transfer contact %(handle)s:'),
            'domain': _('Subject: Request for password for transfer domain name %(handle)s:'),
            'nsset': _('Subject: Request for password for transfer nameserver set %(handle)s:'),
            'keyset': _('Subject: Request for password for transfer keyset %(handle)s:'),
        },
        'block': {
            'contact': _('Subject: Confirmation of the request to block of contact %(handle)s:'),
            'domain': _('Subject: Confirmation of the request to block of domain name %(handle)s:'),
            'nsset': _('Subject: Confirmation of the request to block of nameserver set %(handle)s:'),
            'keyset': _('Subject: Confirmation of the request to block of keyset %(handle)s:'),
        },
        'unblock': {
            'contact': _('Subject: Confirmation of the request to cancel the blocking of contact %(handle)s:'),
            'domain': _('Subject: Confirmation of the request to cancel the blocking of domain name %(handle)s:'),
            'nsset': _('Subject: Confirmation of the request to cancel the blocking of nameserver set %(handle)s:'),
            'keyset': _('Subject: Confirmation of the request to cancel the blocking of keyset %(handle)s:'),
        },
    }
    text_content = {
        'send_password': {
            'contact': _('I hereby confirm my request to get the password for contact %(handle)s, '
                         'submitted through the web form at %(form_url)s on %(created_date)s, assigned id number '
                         '%(response_id)s. Please send the password to %(custom_email)s.'),
            'domain': _('I hereby confirm my request to get the password for domain name %(handle)s, '
                        'submitted through the web form at %(form_url)s on %(created_date)s, assigned id number '
                        '%(response_id)s. Please send the password to %(custom_email)s.'),
            'nsset': _('I hereby confirm my request to get the password for nameserver set %(handle)s, '
                       'submitted through the web form at %(form_url)s on %(created_date)s, assigned id number '
                       '%(response_id)s. Please send the password to %(custom_email)s.'),
            'keyset': _('I hereby confirm my request to get the password for keyset %(handle)s, '
                        'submitted through the web form at %(form_url)s on %(created_date)s, assigned id number '
                        '%(response_id)s. Please send the password to %(custom_email)s.'),
        },
        'block_transfer': {
            'contact': _('I hereby confirm the request to block any change of the sponsoring registrar for the contact '
                         '%(handle)s submitted through the web form on the web site %(form_url)s on %(created_date)s '
                         'with the assigned identification number %(response_id)s, and I request the activation '
                         'of the specified blocking option. I agree that, regarding the particular contact %(handle)s, '
                         'no change of the sponsoring registrar will be possible until I cancel the blocking option '
                         'through the applicable form on %(company_website)s.'),
            'domain': _('I hereby confirm the request to block any change of the sponsoring registrar for the domain '
                        'name %(handle)s submitted through the web form on the web site %(form_url)s on '
                        '%(created_date)s with the assigned identification number %(response_id)s, and I request '
                        'the activation of the specified blocking option. I agree that, regarding the particular '
                        'domain name %(handle)s, no change of the sponsoring registrar will be possible until I cancel '
                        'the blocking option through the applicable form on %(company_website)s.'),
            'nsset': _('I hereby confirm the request to block any change of the sponsoring registrar for '
                       'the nameserver set %(handle)s submitted through the web form on the web site %(form_url)s on '
                       '%(created_date)s with the assigned identification number %(response_id)s, and I request '
                       'the activation of the specified blocking option. I agree that, regarding the particular '
                       'nameserver set %(handle)s, no change of the sponsoring registrar will be possible until '
                       'I cancel the blocking option through the applicable form on %(company_website)s.'),
            'keyset': _('I hereby confirm the request to block any change of the sponsoring registrar for the keyset '
                        '%(handle)s submitted through the web form on the web site %(form_url)s on %(created_date)s '
                        'with the assigned identification number %(response_id)s, and I request the activation '
                        'of the specified blocking option. I agree that, regarding the particular keyset %(handle)s, '
                        'no change of the sponsoring registrar will be possible until I cancel the blocking option '
                        'through the applicable form on %(company_website)s.'),
        },
        'block_all': {
            'contact': _('I hereby confirm the request to block all changes made to contact %(handle)s submitted '
                         'through the web form on the web site %(form_url)s on %(created_date)s with the assigned '
                         'identification number %(response_id)s, and I request the activation of the specified '
                         'blocking option. I agree that, with respect to the particular contact %(handle)s, no change '
                         'will be possible until I cancel the blocking option through the applicable '
                         'form on %(company_website)s.'),
            'domain': _('I hereby confirm the request to block all changes made to domain name %(handle)s submitted '
                        'through the web form on the web site %(form_url)s on %(created_date)s with the assigned '
                        'identification number %(response_id)s, and I request the activation of the specified blocking '
                        'option. I agree that, with respect to the particular domain name %(handle)s, no change '
                        'will be possible until I cancel the blocking option through the applicable '
                        'form on %(company_website)s.'),
            'nsset': _('I hereby confirm the request to block all changes made to nameserver set %(handle)s submitted '
                       'through the web form on the web site %(form_url)s on %(created_date)s with the assigned '
                       'identification number %(response_id)s, and I request the activation of the specified blocking '
                       'option. I agree that, with respect to the particular nameserver set %(handle)s, no change '
                       'will be possible until I cancel the blocking option through the applicable '
                       'form on %(company_website)s.'),
            'keyset': _('I hereby confirm the request to block all changes made to keyset %(handle)s submitted through '
                        'the web form on the web site %(form_url)s on %(created_date)s with the assigned '
                        'identification number %(response_id)s, and I request the activation of the specified blocking '
                        'option. I agree that, with respect to the particular keyset %(handle)s, no change will be '
                        'possible until I cancel the blocking option through the applicable form on '
                        '%(company_website)s.'),
        },
        'unblock_transfer': {
            'contact': _('I hereby confirm the request to cancel the blocking of the sponsoring registrar change '
                         'for the contact %(handle)s submitted through the web form on %(form_url)s on '
                         '%(created_date)s with the assigned identification number %(response_id)s.'),
            'domain': _('I hereby confirm the request to cancel the blocking of the sponsoring registrar change '
                        'for the domain name %(handle)s submitted through the web form on %(form_url)s on '
                        '%(created_date)s with the assigned identification number %(response_id)s.'),
            'nsset': _('I hereby confirm the request to cancel the blocking of the sponsoring registrar change '
                       'for the nameserver set %(handle)s submitted through the web form on %(form_url)s '
                       'on %(created_date)s with the assigned identification number %(response_id)s.'),
            'keyset': _('I hereby confirm the request to cancel the blocking of the sponsoring registrar change '
                        'for the keyset %(handle)s submitted through the web form on %(form_url)s on %(created_date)s '
                        'with the assigned identification number %(response_id)s.'),
        },
        'unblock_all': {
            'contact': _('I hereby confirm the request to cancel the blocking of all changes for the contact '
                         '%(handle)s submitted through the web form on %(form_url)s on %(created_date)s with '
                         'the assigned identification number %(response_id)s.'),
            'domain': _('I hereby confirm the request to cancel the blocking of all changes for the domain name '
                        '%(handle)s submitted through the web form on %(form_url)s on %(created_date)s with '
                        'the assigned identification number %(response_id)s.'),
            'nsset': _('I hereby confirm the request to cancel the blocking of all changes for the nameserver set '
                       '%(handle)s submitted through the web form on %(form_url)s on %(created_date)s with '
                       'the assigned identification number %(response_id)s.'),
            'keyset': _('I hereby confirm the request to cancel the blocking of all changes for the keyset %(handle)s '
                        'submitted through the web form on %(form_url)s on %(created_date)s with the assigned '
                        'identification number %(response_id)s.'),
        },
    }

    def get_context_data(self, **kwargs):
        kwargs.setdefault('company_website', _('the company website'))
        context = super(CustomEmailView, self).get_context_data(**kwargs)
        public_response = self.get_public_response()
        text_context = {'handle': public_response.handle, 'response_id': public_response.public_request_id,
                        'created_date': date_format(public_response.create_date)}
        if getattr(public_response, 'custom_email', None) is not None:
            # Response is a SendPasswordResponse.
            text_context['custom_email'] = public_response.custom_email
            text_context['form_url'] = self.request.build_absolute_uri(reverse('webwhois:form_send_password'))

            title = self.text_title['send_password'][public_response.object_type] % text_context
            context['text_title'] = context['text_header'] = title
            context['text_subject'] = self.text_subject['send_password'][public_response.object_type] % text_context
            context['text_content'] = self.text_content['send_password'][public_response.object_type] % text_context
        else:
            # Response is a BlockResponse.
            assert getattr(public_response, 'lock_type', None)
            if public_response.action == 'block':
                url_name = 'webwhois:form_block_object'
            else:
                url_name = 'webwhois:form_unblock_object'
            text_context['form_url'] = self.request.build_absolute_uri(reverse(url_name))
            text_context['company_website'] = context['company_website']

            action = public_response.action
            title = self.text_title[action][public_response.object_type] % text_context
            context['text_title'] = context['text_header'] = title
            context['text_subject'] = self.text_subject[action][public_response.object_type] % text_context
            key = '%s_%s' % (action, public_response.lock_type)
            context['text_content'] = self.text_content[key][public_response.object_type] % text_context
        return context


class NotarizedLetterView(TextPasswordAndBlockMixin, BaseResponseTemplateView):
    """Notarized letter view."""

    template_name = 'webwhois/public_request_notarized_letter.html'

    def get_context_data(self, **kwargs):
        context = super(NotarizedLetterView, self).get_context_data(**kwargs)
        context['notarized_letter_pdf_url'] = reverse("webwhois:notarized_letter_serve_pdf",
                                                      kwargs={"public_key": kwargs['public_key']},
                                                      current_app=self.request.resolver_match.namespace)

        public_response = self.get_public_response()
        text_context = {'handle': public_response.handle}
        if getattr(public_response, 'custom_email', None) is not None:
            # Response is a SendPasswordResponse.
            title = self.text_title['send_password'][public_response.object_type] % text_context
            context['text_title'] = context['text_header'] = title
            context['pdf_name'] = _("Transfer password request")
        else:
            # Response is a BlockResponse.
            assert getattr(public_response, 'lock_type', None)

            if public_response.action == 'block':
                title = self.text_title['block'][public_response.object_type] % text_context
                context['text_title'] = context['text_header'] = title
                context["pdf_name"] = _("Blocking Request")
            else:
                title = self.text_title['unblock'][public_response.object_type] % text_context
                context['text_title'] = context['text_header'] = title
                context["pdf_name"] = _("Unblocking Request")
        return context


class ServeNotarizedLetterView(PublicRequestLoggerMixin, View):
    """Serve Notarized letter PDF view."""

    def _get_logging_request_name_and_properties(self, data):
        properties = [
            ("handle", data['handle']),
            ("objectType", data['object_type']),
            ("pdfLangCode", data['lang_code']),
            ("documentType", data['document_type']),
        ]
        if 'custom_email' in data:
            properties.append(('customEmail', data['custom_email']))
        return 'NotarizedLetterPdf', properties

    def get(self, request, public_key):
        public_response = cache.get(public_key)
        if public_response is None:
            raise Http404

        registry_lang_codes = {
            'en': Language.en,
            'cs': Language.cs,
        }
        lang_code = get_language()
        if lang_code not in registry_lang_codes:
            lang_code = 'en'
        language_code = registry_lang_codes[lang_code]

        data = {
            'lang_code': lang_code,
            'document_type': public_response.request_type,
            'handle': public_response.handle,
            'object_type': public_response.object_type,
        }
        if getattr(public_response, 'custom_email', None):
            data['custom_email'] = public_response.custom_email
        if LOGGER:
            log_request = self.prepare_logging_request(data)
            log_request_id = log_request.request_id
        else:
            log_request, log_request_id = None, None
        error_object = None
        try:
            pdf_content = PUBLIC_REQUEST.create_public_request_pdf(public_response.public_request_id, language_code)
        except OBJECT_NOT_FOUND as err:
            WEBWHOIS_LOGGING.error('Exception OBJECT_NOT_FOUND risen for public request id %s.' % log_request_id)
            error_object = PublicRequestKnownException(type(err).__name__)
            raise Http404
        except BaseException as error_object:
            raise
        finally:
            self.finish_logging_request(log_request, public_response.public_request_id, error_object)

        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = 'attachment; filename="notarized-letter-{0}.pdf"'.format(lang_code)
        response.content = pdf_content

        return response
